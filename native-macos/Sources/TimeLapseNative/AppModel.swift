import AppKit
import CryptoKit
import Foundation
@preconcurrency import UserNotifications

@MainActor
final class AppModel: ObservableObject {
    @Published var settings: ConnectionSettings
    @Published private(set) var profiles: [ConnectionProfile]
    @Published private(set) var selectedProfileID: UUID?
    @Published var startDate = Date().addingTimeInterval(-24 * 60 * 60)
    @Published var endDate = Date()
    @Published var fullDayMode = false
    @Published private(set) var dailyAutomaticEnabled = false
    @Published var speed = "600x"
    @Published var outputDirectory: URL
    @Published var cameras: [CameraInfo] = []
    @Published var selectedCameraIDs: Set<String> = []
    @Published private(set) var previewCameraID: String?
    @Published var jobs: [DownloadJob] = []
    @Published var logs: [LogEntry] = []
    @Published var statusMessage = "Ready"
    @Published var showingConnectionSheet = false
    @Published var showingCameraSheet = false
    @Published var showingDailyScheduleSheet = false
    @Published var isFirstRun = false
    @Published var alert: AppAlert?
    @Published private(set) var isLoadingCameras = false
    @Published private(set) var thumbnailPreviews: [ThumbnailBoundary: ThumbnailPreview] = [:]

    let speeds = ["1x", "60x", "120x", "300x", "600x"]

    private var cameraProcess: BackendProcess?
    private var cameraRequestID: String?
    private var cameraReceivedTerminal = false
    private var openCameraSheetAfterLoad = false
    private var openDailySheetAfterLoad = false
    private var downloadProcesses: [UUID: BackendProcess] = [:]
    private var thumbnailProcesses: [ThumbnailBoundary: BackendProcess] = [:]
    private var thumbnailRequestIDs: [ThumbnailBoundary: String] = [:]
    private var thumbnailCache: [ThumbnailCacheKey: CachedThumbnail] = [:]
    private var downloadReceivedTerminal: Set<UUID> = []
    private var rateLimitedDownloadQueue: [UUID] = []
    private var handledQueuedCancellations: Set<UUID> = []
    private var reservedOutputPaths: Set<String> = []
    private var nextGroupNumber = 1
    private var didStart = false
    private var isShuttingDown = false
    private var shutdownCompletion: (() -> Void)?
    private let initialCredentialAlert: AppAlert?
    private let initialMigrationMessage: String?
    private var editingProfileID: UUID?
    private var dailySchedule: DailySchedule?
    private var dailyTimer: Timer?
    private var didReportNotificationIssue = false

    var isBusy: Bool { isLoadingCameras || !downloadProcesses.isEmpty }
    var hasActiveDownloadJobs: Bool { !downloadProcesses.isEmpty || !rateLimitedDownloadQueue.isEmpty }
    var hasActiveBackendProcesses: Bool {
        cameraProcess != nil || !downloadProcesses.isEmpty || !thumbnailProcesses.isEmpty
    }
    func hasClearableJobs(dailyAutomations: Bool) -> Bool {
        jobs.contains { $0.isDailySchedule == dailyAutomations && $0.state.isTerminal }
    }

    func hasCancellableJobs(dailyAutomations: Bool) -> Bool {
        jobs.contains {
            $0.isDailySchedule == dailyAutomations && !$0.state.isTerminal && $0.state != .cancelling
        }
    }
    var selectedProfile: ConnectionProfile? {
        profiles.first { $0.id == selectedProfileID }
    }

    var profileBeingEdited: ConnectionProfile? {
        profiles.first { $0.id == editingProfileID }
    }

    var selectedCameras: [CameraInfo] {
        cameras.filter { selectedCameraIDs.contains($0.id) }
    }

    var previewCamera: CameraInfo? {
        selectedCameras.first { $0.id == previewCameraID }
    }

    var cameraSummary: String {
        switch selectedCameras.count {
        case 0: "No cameras selected"
        case 1: selectedCameras[0].name
        default: "\(selectedCameras.count) cameras selected"
        }
    }

    private struct DailySchedule {
        let cameras: [CameraInfo]
        let outputDirectory: URL
        let settings: BackendSettings
        let speed: String
        let jobID: UUID
        var lastRunDay: Date?
        var activeDay: Date?
        var activeJobIDs: Set<UUID>
    }

    private struct ThumbnailCacheKey: Hashable {
        let cameraID: String
        let timestamp: Date
    }

    private struct CachedThumbnail {
        let imageData: Data
        let source: String?
    }

    init() {
        let loaded: ProfileLoadResult
        do {
            loaded = try ProfileStore.load()
            initialCredentialAlert = loaded.warning.map {
                AppAlert(title: "Credential Storage Warning", message: $0)
            }
            initialMigrationMessage = loaded.migrationMessage
        } catch {
            loaded = ProfileLoadResult(state: ConnectionProfileState(), migrationMessage: nil, warning: nil)
            initialCredentialAlert = AppAlert(
                title: "Could Not Read Profiles",
                message: "The saved connection profiles could not be read from the macOS Keychain: \(error.localizedDescription)"
            )
            initialMigrationMessage = nil
        }
        let state = loaded.state.normalized
        profiles = state.profiles
        selectedProfileID = state.selectedProfileID
        settings = state.profiles.first { $0.id == state.selectedProfileID }?.settings ?? ConnectionSettings()
        isFirstRun = state.profiles.isEmpty
        let savedOutput = UserDefaults.standard.string(forKey: "output_directory")
        if let savedOutput, !savedOutput.isEmpty {
            outputDirectory = URL(fileURLWithPath: savedOutput, isDirectory: true)
        } else {
            outputDirectory = FileManager.default.urls(for: .moviesDirectory, in: .userDomainMask).first?
                .appendingPathComponent("TimeLapse", isDirectory: true)
                ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Movies/TimeLapse", isDirectory: true)
        }
    }

    func start() {
        guard !didStart else { return }
        didStart = true
        showingConnectionSheet = isFirstRun
        appendLog(level: "INFO", message: "Application ready")
        if let initialMigrationMessage {
            statusMessage = "Connection moved into a profile"
            appendLog(level: "INFO", message: initialMigrationMessage)
        }
        if let initialCredentialAlert {
            statusMessage = initialCredentialAlert.title
            alert = initialCredentialAlert
            appendLog(level: "ERROR", message: initialCredentialAlert.message)
        }
    }

    func presentConnectionSettings() {
        guard !isLoadingCameras else {
            alert = AppAlert(title: "Loading Cameras", message: "Wait for the current camera refresh to finish.")
            return
        }
        guard let selectedProfileID else {
            presentNewProfile()
            return
        }
        editingProfileID = selectedProfileID
        isFirstRun = false
        showingConnectionSheet = true
    }

    func presentNewProfile() {
        guard !isLoadingCameras else {
            alert = AppAlert(title: "Loading Cameras", message: "Wait for the current camera refresh to finish.")
            return
        }
        editingProfileID = nil
        isFirstRun = profiles.isEmpty
        showingConnectionSheet = true
    }

    func selectProfile(_ profileID: UUID) {
        guard profileID != selectedProfileID else { return }
        guard !isLoadingCameras else {
            alert = AppAlert(title: "Loading Cameras", message: "Wait for the current camera refresh to finish.")
            return
        }
        guard let profile = profiles.first(where: { $0.id == profileID }) else { return }
        let state = ConnectionProfileState(profiles: profiles, selectedProfileID: profileID)
        do {
            try ProfileStore.save(state)
        } catch {
            alert = AppAlert(title: "Could Not Select Profile", message: error.localizedDescription)
            return
        }
        selectedProfileID = profileID
        settings = profile.settings
        cameras.removeAll()
        selectedCameraIDs.removeAll()
        previewCameraID = nil
        clearThumbnailPreviews()
        statusMessage = "Selected \(profile.displayName)"
        appendLog(level: "INFO", message: "Selected connection profile: \(profile.displayName)")
    }

    func saveConnection(profileName: String, settings candidate: ConnectionSettings) -> String? {
        if let error = candidate.validationError { return error }
        let profile = ConnectionProfile(
            id: editingProfileID ?? UUID(),
            name: profileName,
            settings: candidate
        ).normalized
        var updatedProfiles = profiles
        if let index = updatedProfiles.firstIndex(where: { $0.id == profile.id }) {
            updatedProfiles[index] = profile
        } else {
            updatedProfiles.append(profile)
        }
        let state = ConnectionProfileState(profiles: updatedProfiles, selectedProfileID: profile.id)
        do {
            try ProfileStore.save(state)
        } catch {
            return "The connection profile could not be saved: \(error.localizedDescription)"
        }
        profiles = state.normalized.profiles
        selectedProfileID = profile.id
        settings = profile.settings
        cameras.removeAll()
        selectedCameraIDs.removeAll()
        previewCameraID = nil
        clearThumbnailPreviews()
        editingProfileID = nil
        isFirstRun = false
        showingConnectionSheet = false
        statusMessage = "Saved \(profile.displayName)"
        appendLog(level: "INFO", message: "Saved connection profile: \(profile.displayName)")
        return nil
    }

    func cancelConnectionSheet() {
        if isFirstRun {
            showingConnectionSheet = false
            DispatchQueue.main.async {
                NSApplication.shared.terminate(nil)
            }
        } else {
            editingProfileID = nil
            showingConnectionSheet = false
        }
    }

    func chooseOutputDirectory() {
        let panel = NSOpenPanel()
        panel.title = "Choose Output Folder"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = outputDirectory
        guard panel.runModal() == .OK, let selected = panel.url else { return }
        outputDirectory = selected
        UserDefaults.standard.set(selected.path, forKey: "output_directory")
    }

    func requestCameraSelection() {
        if cameras.isEmpty {
            loadCameras(openSelection: true)
        } else {
            showingCameraSheet = true
        }
    }

    func applyCameraSelection(_ ids: Set<String>) {
        selectedCameraIDs = ids
        normalizePreviewCameraSelection()
        clearThumbnailPreviews()
        showingCameraSheet = false
        appendLog(level: "INFO", message: "Selected \(ids.count) cameras")
        if !selectedCameras.isEmpty {
            requestThumbnail(for: .start)
            requestThumbnail(for: .end)
        }
    }

    func setFullDayStart(_ date: Date) {
        let start = Calendar.current.startOfDay(for: date)
        startDate = start
        endDate = Calendar.current.date(byAdding: .day, value: 1, to: start) ?? start.addingTimeInterval(24 * 60 * 60)
    }

    func setFullDayEnd(_ date: Date) {
        let end = Calendar.current.startOfDay(for: date)
        endDate = end
        startDate = Calendar.current.date(byAdding: .day, value: -1, to: end) ?? end.addingTimeInterval(-24 * 60 * 60)
    }

    func setFullDayMode(_ enabled: Bool) {
        fullDayMode = enabled
        if enabled { setFullDayStart(startDate) }
    }

    func selectPreviewCamera(_ cameraID: String?) {
        guard cameraID != previewCameraID,
              let cameraID,
              selectedCameraIDs.contains(cameraID) else { return }
        previewCameraID = cameraID
        clearThumbnailPreviews(clearCache: false)
        requestThumbnail(for: .start)
        requestThumbnail(for: .end)
    }

    func requestThumbnail(for boundary: ThumbnailBoundary) {
        let selectedDate = boundary == .start ? startDate : endDate
        let timestamp = fullDayMode ? Calendar.current.startOfDay(for: selectedDate) : selectedDate
        guard let camera = previewCamera else {
            thumbnailPreviews[boundary] = ThumbnailPreview(
                timestamp: timestamp,
                cameraID: nil,
                cameraName: nil,
                imageData: nil,
                source: nil,
                message: "Select a camera to preview this time.",
                isLoading: false
            )
            return
        }
        if let preview = thumbnailPreviews[boundary],
           preview.timestamp == timestamp,
           preview.cameraID == camera.id {
            return
        }
        let cacheKey = ThumbnailCacheKey(cameraID: camera.id, timestamp: timestamp)
        if let cached = thumbnailCache[cacheKey] {
            thumbnailProcesses[boundary]?.cancel()
            thumbnailProcesses.removeValue(forKey: boundary)
            thumbnailRequestIDs.removeValue(forKey: boundary)
            thumbnailPreviews[boundary] = ThumbnailPreview(
                timestamp: timestamp,
                cameraID: camera.id,
                cameraName: camera.name,
                imageData: cached.imageData,
                source: cached.source,
                message: nil,
                isLoading: false
            )
            return
        }

        thumbnailProcesses[boundary]?.cancel()
        let id = UUID().uuidString
        let process = BackendProcess()
        thumbnailProcesses[boundary] = process
        thumbnailRequestIDs[boundary] = id
        thumbnailPreviews[boundary] = ThumbnailPreview(
            timestamp: timestamp,
            cameraID: camera.id,
            cameraName: camera.name,
            imageData: nil,
            source: nil,
            message: nil,
            isLoading: true
        )
        let request = ThumbnailRequest(
            id: id,
            settings: BackendSettings(settings),
            camera: camera,
            timestamp: Self.iso8601String(timestamp)
        )
        do {
            try process.start(
                request: request,
                onEvent: { [weak self] event in
                    self?.handleThumbnailEvent(event, boundary: boundary, requestID: id)
                },
                onCompletion: { [weak self] completion in
                    self?.finishThumbnail(completion, boundary: boundary, requestID: id)
                }
            )
        } catch {
            thumbnailProcesses.removeValue(forKey: boundary)
            thumbnailRequestIDs.removeValue(forKey: boundary)
            thumbnailPreviews[boundary]?.isLoading = false
            thumbnailPreviews[boundary]?.message = error.localizedDescription
        }
    }

    func requestDailySchedule() {
        guard !dailyAutomaticEnabled else { return }
        if cameras.isEmpty {
            openCameraSheetAfterLoad = false
            openDailySheetAfterLoad = true
            loadCameras()
        } else {
            showingDailyScheduleSheet = true
        }
    }

    func cancelDailyScheduleSheet() {
        showingDailyScheduleSheet = false
        openDailySheetAfterLoad = false
    }

    func configureDailySchedule(cameraIDs: Set<String>, outputDirectory: URL) {
        guard dailySchedule == nil else { return }
        let selected = cameras.filter { cameraIDs.contains($0.id) }
        guard !selected.isEmpty else {
            alert = AppAlert(title: "No Cameras Selected", message: "Select at least one camera for the daily job.")
            return
        }
        var isDirectory: ObjCBool = false
        if FileManager.default.fileExists(atPath: outputDirectory.path, isDirectory: &isDirectory), !isDirectory.boolValue {
            alert = AppAlert(title: "Invalid Output Folder", message: "The selected output location is not a folder.")
            return
        }
        do {
            try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        } catch {
            alert = AppAlert(title: "Could Not Create Output Folder", message: error.localizedDescription)
            return
        }
        let group = nextGroupNumber
        nextGroupNumber += 1
        let camera = CameraInfo(
            id: "daily-schedule-\(UUID().uuidString)",
            name: selected.count == 1 ? selected[0].name : "\(selected.count) cameras",
            state: nil,
            model: nil
        )
        let job = DownloadJob(
            groupNumber: group,
            camera: camera,
            outputURL: outputDirectory,
            requestSettings: BackendSettings(settings),
            requestStart: "",
            requestEnd: "",
            requestSpeed: speed,
            isDailySchedule: true,
            initialState: .scheduled
        )
        jobs.append(job)
        dailySchedule = DailySchedule(
            cameras: selected,
            outputDirectory: outputDirectory,
            settings: BackendSettings(settings),
            speed: speed,
            jobID: job.id,
            lastRunDay: nil,
            activeDay: nil,
            activeJobIDs: []
        )
        dailyAutomaticEnabled = true
        showingDailyScheduleSheet = false
        statusMessage = "Scheduled daily timelapses for \(selected.count) cameras"
        appendLog(level: "INFO", message: statusMessage)
        startDailyTimer()
        runDailyScheduleIfDue()
    }

    func stopDailySchedule() {
        guard let schedule = dailySchedule else { return }
        dailyTimer?.invalidate()
        dailyTimer = nil
        dailySchedule = nil
        dailyAutomaticEnabled = false
        jobs.first { $0.id == schedule.jobID }?.state = .stopped
        statusMessage = "Stopped daily automatic timelapses"
        appendLog(level: "INFO", message: statusMessage)
    }

    private func startDailyTimer() {
        dailyTimer?.invalidate()
        dailyTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.runDailyScheduleIfDue()
            }
        }
    }

    private func runDailyScheduleIfDue() {
        guard var schedule = dailySchedule else { return }
        if !schedule.activeJobIDs.isEmpty {
            if schedule.activeJobIDs.contains(where: { id in
                downloadProcesses[id] != nil || jobs.first(where: { $0.id == id })?.state == .queued
            }) { return }
            let tracked = jobs.filter { schedule.activeJobIDs.contains($0.id) }
            let completed = tracked.count == schedule.activeJobIDs.count
                && tracked.allSatisfy { $0.state == .completed && Self.isValidExport($0.outputURL) }
            if completed { schedule.lastRunDay = schedule.activeDay }
            schedule.activeDay = nil
            schedule.activeJobIDs.removeAll()
            dailySchedule = schedule
            if !completed { return }
        }
        let calendar = Calendar.current
        let today = calendar.startOfDay(for: Date())
        let firstDay = schedule.lastRunDay.flatMap { calendar.date(byAdding: .day, value: 1, to: $0) }
            ?? calendar.date(byAdding: .day, value: -1, to: today)
        guard var day = firstDay else { return }
        while day < today {
            guard let end = calendar.date(byAdding: .day, value: 1, to: day) else { break }
            let missing = schedule.cameras.filter {
                !Self.isValidExport(Self.expectedOutputURL(
                    camera: $0,
                    start: day,
                    end: end,
                    speed: schedule.speed,
                    outputDirectory: schedule.outputDirectory,
                    daily: true,
                    fullDay: true
                ))
            }
            if missing.isEmpty {
                schedule.lastRunDay = day
                dailySchedule = schedule
                day = end
                continue
            }
            let group = nextGroupNumber
            nextGroupNumber += 1
            schedule.activeDay = day
            for camera in missing {
                let job = startDownload(
                    camera: camera,
                    group: group,
                    start: day,
                    end: end,
                    speed: schedule.speed,
                    outputDirectory: schedule.outputDirectory,
                    requestSettings: schedule.settings,
                    daily: true
                )
                schedule.activeJobIDs.insert(job.id)
            }
            dailySchedule = schedule
            statusMessage = "Started daily job \(group) for \(Self.calendarDay(day))"
            appendLog(level: "INFO", message: statusMessage)
            return
        }
    }

    func loadCameras(openSelection: Bool = false) {
        guard !isLoadingCameras else { return }
        guard settings.validationError == nil else {
            openDailySheetAfterLoad = false
            isFirstRun = true
            showingConnectionSheet = true
            return
        }
        let id = UUID().uuidString
        let process = BackendProcess()
        cameraProcess = process
        cameraRequestID = id
        cameraReceivedTerminal = false
        openCameraSheetAfterLoad = openSelection
        isLoadingCameras = true
        statusMessage = "Loading cameras…"
        appendLog(level: "INFO", message: "Loading cameras")
        let request = ListCamerasRequest(id: id, settings: BackendSettings(settings))
        do {
            try process.start(
                request: request,
                onEvent: { [weak self] event in self?.handleCameraEvent(event) },
                onCompletion: { [weak self] completion in self?.finishCameraLoad(completion) }
            )
        } catch {
            cameraProcess = nil
            isLoadingCameras = false
            openDailySheetAfterLoad = false
            showError(title: "Could Not Load Cameras", message: error.localizedDescription)
        }
    }

    func startDownloads() {
        guard !selectedCameras.isEmpty else { return }
        guard endDate > startDate else {
            alert = AppAlert(title: "Invalid Date Range", message: "The end date and time must be after the start.")
            return
        }
        var isDirectory: ObjCBool = false
        if FileManager.default.fileExists(atPath: outputDirectory.path, isDirectory: &isDirectory), !isDirectory.boolValue {
            alert = AppAlert(title: "Invalid Output Folder", message: "The selected output location is not a folder.")
            return
        }
        let group = nextGroupNumber
        nextGroupNumber += 1
        for camera in selectedCameras {
            startDownload(
                camera: camera,
                group: group,
                start: startDate,
                end: endDate,
                speed: speed,
                outputDirectory: outputDirectory,
                requestSettings: BackendSettings(settings),
                fullDay: fullDayMode
            )
        }
        statusMessage = "Started job \(group) with \(selectedCameras.count) downloads"
        appendLog(level: "INFO", message: statusMessage)
    }

    func cancel(_ job: DownloadJob) {
        if job.isDailySchedule {
            stopDailySchedule()
            return
        }
        guard !job.state.isTerminal, job.state != .cancelling else { return }
        if job.state == .queued {
            cancelQueuedDownload(job)
            return
        }
        job.state = .cancelling
        downloadProcesses[job.id]?.cancel()
        appendLog(level: "INFO", message: "Cancellation requested for \(job.camera.name)")
    }

    func cancelAllJobs(dailyAutomations: Bool) {
        let cancellableJobs = jobs.filter {
            $0.isDailySchedule == dailyAutomations && !$0.state.isTerminal && $0.state != .cancelling
        }
        guard !cancellableJobs.isEmpty else { return }
        for job in cancellableJobs where job.state == .queued {
            cancelQueuedDownload(job, advanceQueue: false)
        }
        for job in cancellableJobs where job.state != .queued {
            cancel(job)
        }
        statusMessage = dailyAutomations
            ? "Stopping \(cancellableJobs.count) daily automations…"
            : "Cancelling \(cancellableJobs.count) downloads…"
    }

    @discardableResult
    func remove(_ job: DownloadJob) -> Bool {
        guard job.state.isTerminal else { return false }
        guard deleteOutput(for: job) else { return false }
        jobs.removeAll { $0.id == job.id }
        statusMessage = "Deleted \(job.camera.name) from the job list"
        appendLog(level: "INFO", message: statusMessage)
        return true
    }

    func clearFinishedJobs(dailyAutomations: Bool) {
        let clearableJobs = jobs.filter {
            $0.isDailySchedule == dailyAutomations && $0.state.isTerminal
        }
        var deletedCount = 0
        for job in clearableJobs where remove(job) {
            deletedCount += 1
        }
        guard deletedCount > 0 else { return }
        statusMessage = dailyAutomations
            ? "Deleted \(deletedCount) stopped daily automations"
            : "Deleted \(deletedCount) finished downloads"
        appendLog(level: "INFO", message: statusMessage)
    }

    private func deleteOutput(for job: DownloadJob) -> Bool {
        guard !job.isDailySchedule else { return true }
        var isDirectory = ObjCBool(false)
        guard FileManager.default.fileExists(atPath: job.outputURL.path, isDirectory: &isDirectory) else {
            return true
        }
        guard !isDirectory.boolValue else {
            reportDeleteFailure(job, detail: "The output path is a directory and was left untouched.")
            return false
        }
        do {
            try FileManager.default.removeItem(at: job.outputURL)
            return true
        } catch {
            reportDeleteFailure(job, detail: error.localizedDescription)
            return false
        }
    }

    private func reportDeleteFailure(_ job: DownloadJob, detail: String) {
        let message = "The exported video could not be deleted from \(job.outputURL.path): \(detail)"
        alert = AppAlert(title: "Could Not Delete Export", message: message)
        statusMessage = "Could not delete \(job.camera.name)"
        appendLog(level: "ERROR", message: message)
    }

    func restart(_ job: DownloadJob) {
        guard !job.isDailySchedule else { return }
        guard job.state == .cancelled || job.state.isFailure, downloadProcesses[job.id] == nil else { return }
        guard !FileManager.default.fileExists(atPath: job.outputURL.path) else {
            alert = AppAlert(
                title: "Output Already Exists",
                message: "Move or remove \(job.outputURL.lastPathComponent) before restarting this job."
            )
            return
        }
        reservedOutputPaths.insert(job.outputURL.path.lowercased())
        job.state = .preparing
        job.downloadedBytes = 0
        job.totalBytes = nil
        job.bytesPerSecond = 0
        job.elapsedSeconds = 0
        job.lastProgressAt = nil
        launchDownload(job)
        statusMessage = "Restarted download for \(job.camera.name)"
        appendLog(level: "INFO", message: statusMessage)
    }

    func reveal(_ job: DownloadJob) {
        if FileManager.default.fileExists(atPath: job.outputURL.path) {
            NSWorkspace.shared.activateFileViewerSelecting([job.outputURL])
        } else {
            NSWorkspace.shared.open(job.outputURL.deletingLastPathComponent())
        }
    }

    func openVideo(_ job: DownloadJob) {
        guard job.state == .completed else { return }
        guard FileManager.default.fileExists(atPath: job.outputURL.path) else {
            alert = AppAlert(
                title: "Video Not Found",
                message: "The completed video could not be found at \(job.outputURL.path)."
            )
            appendLog(level: "ERROR", message: "Completed video is missing: \(job.outputURL.path)")
            return
        }
        NSWorkspace.shared.open(job.outputURL)
    }

    func clearLogs() {
        logs.removeAll()
    }

    func reportNotificationIssue(_ message: String) {
        appendLog(level: "WARNING", message: message)
        guard !didReportNotificationIssue else { return }
        didReportNotificationIssue = true
        statusMessage = "Notifications unavailable"
        alert = AppAlert(title: "Notifications Are Disabled", message: message)
    }

    func shutdown(completion: @escaping () -> Void) {
        guard !isShuttingDown else { return }
        isShuttingDown = true
        shutdownCompletion = completion
        dailyTimer?.invalidate()
        dailyTimer = nil
        dailySchedule = nil
        for id in rateLimitedDownloadQueue {
            guard let job = jobs.first(where: { $0.id == id }), job.state == .queued else { continue }
            job.state = .cancelled
            releaseOutputURL(job.outputURL)
        }
        rateLimitedDownloadQueue.removeAll()
        cameraProcess?.cancel()
        for process in downloadProcesses.values {
            process.cancel()
        }
        for process in thumbnailProcesses.values {
            process.cancel()
        }
        finishShutdownIfReady()
    }

    private func clearThumbnailPreviews(clearCache: Bool = true) {
        for process in thumbnailProcesses.values {
            process.cancel()
        }
        thumbnailProcesses.removeAll()
        thumbnailRequestIDs.removeAll()
        thumbnailPreviews.removeAll()
        if clearCache {
            thumbnailCache.removeAll()
        }
    }

    private func normalizePreviewCameraSelection() {
        if let previewCameraID, selectedCameraIDs.contains(previewCameraID) {
            return
        }
        previewCameraID = selectedCameras.first?.id
    }

    private func handleThumbnailEvent(_ event: BackendEvent, boundary: ThumbnailBoundary, requestID: String) {
        guard thumbnailRequestIDs[boundary] == requestID,
              event.id == nil || event.id == requestID else { return }
        switch event.event {
        case "thumbnail":
            guard let encoded = event.thumbnailBase64, let data = Data(base64Encoded: encoded) else {
                thumbnailPreviews[boundary]?.isLoading = false
                thumbnailPreviews[boundary]?.message = "The thumbnail data could not be read."
                return
            }
            thumbnailPreviews[boundary]?.imageData = data
            thumbnailPreviews[boundary]?.source = event.thumbnailSource
            thumbnailPreviews[boundary]?.message = nil
            thumbnailPreviews[boundary]?.isLoading = false
            if let preview = thumbnailPreviews[boundary], let cameraID = preview.cameraID {
                let key = ThumbnailCacheKey(cameraID: cameraID, timestamp: preview.timestamp)
                thumbnailCache[key] = CachedThumbnail(imageData: data, source: event.thumbnailSource)
            }
        case "error":
            thumbnailPreviews[boundary]?.isLoading = false
            thumbnailPreviews[boundary]?.message = event.message ?? "No thumbnail is available for this time."
        case "log":
            appendLog(level: event.level ?? "INFO", message: event.message ?? "")
        default:
            break
        }
    }

    private func finishThumbnail(
        _ completion: BackendCompletion,
        boundary: ThumbnailBoundary,
        requestID: String
    ) {
        guard thumbnailRequestIDs[boundary] == requestID else { return }
        thumbnailProcesses.removeValue(forKey: boundary)
        thumbnailRequestIDs.removeValue(forKey: boundary)
        if thumbnailPreviews[boundary]?.isLoading == true {
            thumbnailPreviews[boundary]?.isLoading = false
            if !completion.wasCancelled {
                thumbnailPreviews[boundary]?.message = completion.stderr.isEmpty
                    ? "No thumbnail is available for this time."
                    : completion.stderr
            }
        }
        finishShutdownIfReady()
    }

    private func handleCameraEvent(_ event: BackendEvent) {
        guard event.id == nil || event.id == cameraRequestID else { return }
        switch event.event {
        case "cameras":
            cameraReceivedTerminal = true
            cameras = event.cameras ?? []
            let validIDs = Set(cameras.map(\.id))
            selectedCameraIDs.formIntersection(validIDs)
            normalizePreviewCameraSelection()
            statusMessage = "Loaded \(cameras.count) cameras"
            appendLog(level: "INFO", message: statusMessage)
            if cameras.isEmpty {
                openDailySheetAfterLoad = false
                alert = AppAlert(title: "No Cameras", message: "No cameras were returned by UniFi Protect.")
            } else if openCameraSheetAfterLoad {
                showingCameraSheet = true
            } else if openDailySheetAfterLoad {
                openDailySheetAfterLoad = false
                showingDailyScheduleSheet = true
            }
        case "error":
            cameraReceivedTerminal = true
            openDailySheetAfterLoad = false
            showError(title: "Could Not Load Cameras", message: event.message ?? "An unknown backend error occurred.")
        case "log":
            appendLog(level: event.level ?? "INFO", message: event.message ?? "")
        case "complete", "cancelled":
            cameraReceivedTerminal = true
        default:
            appendLog(level: "WARNING", message: "Unknown backend event: \(event.event)")
        }
    }

    private func finishCameraLoad(_ completion: BackendCompletion) {
        cameraProcess = nil
        isLoadingCameras = false
        if !cameraReceivedTerminal && !completion.wasCancelled {
            openDailySheetAfterLoad = false
            let detail = completion.stderr.isEmpty
                ? "The backend exited with status \(completion.exitCode) without returning a camera list."
                : completion.stderr
            showError(title: "Could Not Load Cameras", message: detail)
        }
        finishShutdownIfReady()
    }

    @discardableResult
    private func startDownload(
        camera: CameraInfo,
        group: Int,
        start: Date,
        end: Date,
        speed: String,
        outputDirectory: URL,
        requestSettings: BackendSettings,
        daily: Bool = false,
        fullDay: Bool = false
    ) -> DownloadJob {
        let outputURL = reserveOutputURL(
            camera: camera,
            start: start,
            end: end,
            speed: speed,
            outputDirectory: outputDirectory,
            daily: daily,
            fullDay: fullDay
        )
        let job = DownloadJob(
            groupNumber: group,
            camera: camera,
            outputURL: outputURL,
            requestSettings: requestSettings,
            requestStart: Self.iso8601String(start),
            requestEnd: Self.iso8601String(end),
            requestSpeed: speed
        )
        jobs.append(job)
        launchDownload(job)
        return job
    }

    private func launchDownload(_ job: DownloadJob) {
        rateLimitedDownloadQueue.removeAll { $0 == job.id }
        job.state = .preparing
        let process = BackendProcess()
        downloadProcesses[job.id] = process
        downloadReceivedTerminal.remove(job.id)
        let request = DownloadRequest(
            id: job.id.uuidString,
            settings: job.requestSettings,
            camera: job.camera,
            start: job.requestStart,
            end: job.requestEnd,
            speed: job.requestSpeed,
            output: job.outputURL.path
        )
        appendLog(level: "INFO", message: "Started camera download: \(job.camera.name) -> \(job.outputURL.path)")
        do {
            try process.start(
                request: request,
                onEvent: { [weak self, weak job] event in
                    guard let self, let job else { return }
                    self.handleDownloadEvent(event, job: job)
                },
                onCompletion: { [weak self, weak job] completion in
                    guard let self, let job else { return }
                    self.finishDownload(completion, job: job)
                }
            )
        } catch {
            job.state = .failed(error.localizedDescription)
            downloadReceivedTerminal.insert(job.id)
            downloadProcesses.removeValue(forKey: job.id)
            releaseOutputURL(job.outputURL)
            appendLog(level: "ERROR", message: "Download failed for \(job.camera.name): \(error.localizedDescription)")
            sendDownloadNotification(
                title: "Download Failed",
                message: "\(job.camera.name): \(error.localizedDescription)"
            )
        }
    }

    private func handleDownloadEvent(_ event: BackendEvent, job: DownloadJob) {
        guard event.id == nil || event.id == job.id.uuidString else { return }
        switch event.event {
        case "progress":
            if job.state != .cancelling { job.state = .downloading }
            job.downloadedBytes = event.downloadedBytes ?? job.downloadedBytes
            job.totalBytes = event.totalBytes ?? job.totalBytes
            job.bytesPerSecond = event.bytesPerSecond ?? job.bytesPerSecond
            job.elapsedSeconds = event.elapsedSeconds ?? job.elapsedSeconds
            job.lastProgressAt = Date()
        case "complete":
            job.state = .completed
            job.bytesPerSecond = 0
            downloadReceivedTerminal.insert(job.id)
            appendLog(level: "INFO", message: "Completed camera download: \(job.camera.name)")
            sendDownloadNotification(
                title: "Download Complete",
                message: "\(job.camera.name): \(job.outputURL.lastPathComponent)"
            )
        case "cancelled":
            job.state = .cancelled
            job.bytesPerSecond = 0
            downloadReceivedTerminal.insert(job.id)
            appendLog(level: "INFO", message: "Cancelled camera download: \(job.camera.name)")
            sendDownloadNotification(
                title: "Download Interrupted",
                message: "The download for \(job.camera.name) was cancelled."
            )
        case "error":
            let message = event.message ?? "An unknown backend error occurred."
            job.bytesPerSecond = 0
            downloadReceivedTerminal.insert(job.id)
            if event.code == "protect_rate_limited" {
                job.state = .queued
                if !rateLimitedDownloadQueue.contains(job.id) {
                    rateLimitedDownloadQueue.append(job.id)
                }
                appendLog(
                    level: "WARNING",
                    message: "Queued rate-limited download for \(job.camera.name); waiting for a completion or cancellation."
                )
                statusMessage = "Queued \(job.camera.name) after Protect returned HTTP 429"
            } else {
                job.state = .failed(message)
                appendLog(level: "ERROR", message: "Download failed for \(job.camera.name): \(message)")
                sendDownloadNotification(title: "Download Failed", message: "\(job.camera.name): \(message)")
            }
        case "log":
            appendLog(level: event.level ?? "INFO", message: event.message ?? "")
        default:
            appendLog(level: "WARNING", message: "Unknown backend event: \(event.event)")
        }
    }

    private func finishDownload(_ completion: BackendCompletion, job: DownloadJob) {
        if !downloadReceivedTerminal.contains(job.id) {
            if completion.wasCancelled {
                job.state = .cancelled
                appendLog(level: "INFO", message: "Cancelled camera download: \(job.camera.name)")
                sendDownloadNotification(
                    title: "Download Interrupted",
                    message: "The download for \(job.camera.name) was cancelled."
                )
            } else {
                let detail = completion.stderr.isEmpty
                    ? "The backend exited with status \(completion.exitCode) without a completion event."
                    : completion.stderr
                job.state = .failed(detail)
                appendLog(level: "ERROR", message: "Download failed for \(job.camera.name): \(detail)")
                sendDownloadNotification(title: "Download Failed", message: "\(job.camera.name): \(detail)")
            }
        }
        job.bytesPerSecond = 0
        downloadProcesses.removeValue(forKey: job.id)
        downloadReceivedTerminal.remove(job.id)
        let queuedCancellationWasHandled = handledQueuedCancellations.remove(job.id) != nil
        if job.state != .queued {
            releaseOutputURL(job.outputURL)
        }
        let shouldAdvanceQueue = (job.state == .completed || job.state == .cancelled)
            && !queuedCancellationWasHandled
        if downloadProcesses.isEmpty {
            statusMessage = rateLimitedDownloadQueue.isEmpty
                ? "Ready"
                : "\(rateLimitedDownloadQueue.count) downloads queued"
        } else {
            statusMessage = "\(downloadProcesses.count) downloads active"
        }
        if shouldAdvanceQueue {
            retryNextRateLimitedDownload()
        }
        finishShutdownIfReady()
    }

    private func cancelQueuedDownload(_ job: DownloadJob, advanceQueue: Bool = true) {
        guard job.state == .queued else { return }
        if downloadProcesses[job.id] != nil {
            handledQueuedCancellations.insert(job.id)
        }
        rateLimitedDownloadQueue.removeAll { $0 == job.id }
        job.state = .cancelled
        job.bytesPerSecond = 0
        releaseOutputURL(job.outputURL)
        appendLog(level: "INFO", message: "Cancelled queued camera download: \(job.camera.name)")
        sendDownloadNotification(
            title: "Download Interrupted",
            message: "The queued download for \(job.camera.name) was cancelled."
        )
        if advanceQueue {
            retryNextRateLimitedDownload()
        }
    }

    private func retryNextRateLimitedDownload() {
        guard !isShuttingDown else { return }
        while !rateLimitedDownloadQueue.isEmpty {
            let id = rateLimitedDownloadQueue.removeFirst()
            guard let job = jobs.first(where: { $0.id == id }), job.state == .queued else { continue }
            job.downloadedBytes = 0
            job.totalBytes = nil
            job.bytesPerSecond = 0
            job.elapsedSeconds = 0
            job.lastProgressAt = nil
            launchDownload(job)
            statusMessage = "Retrying queued download for \(job.camera.name)"
            appendLog(level: "INFO", message: statusMessage)
            return
        }
    }

    private func reserveOutputURL(
        camera: CameraInfo,
        start: Date,
        end: Date,
        speed: String,
        outputDirectory: URL,
        daily: Bool,
        fullDay: Bool
    ) -> URL {
        let expected = Self.expectedOutputURL(
            camera: camera,
            start: start,
            end: end,
            speed: speed,
            outputDirectory: outputDirectory,
            daily: daily,
            fullDay: fullDay
        )
        if daily {
            reservedOutputPaths.insert(expected.path.lowercased())
            return expected
        }
        let base = expected.deletingPathExtension().lastPathComponent
        var counter = 1
        while true {
            let suffix = counter == 1 ? "" : "_\(counter)"
            let candidate = outputDirectory.appendingPathComponent("\(base)\(suffix).mp4")
            let key = candidate.path.lowercased()
            if !FileManager.default.fileExists(atPath: candidate.path), !reservedOutputPaths.contains(key) {
                reservedOutputPaths.insert(key)
                return candidate
            }
            counter += 1
        }
    }

    private func releaseOutputURL(_ url: URL) {
        reservedOutputPaths.remove(url.path.lowercased())
    }

    private static func expectedOutputURL(
        camera: CameraInfo,
        start: Date,
        end: Date,
        speed: String,
        outputDirectory: URL,
        daily: Bool,
        fullDay: Bool
    ) -> URL {
        let digest = SHA256.hash(data: Data(camera.id.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
            .prefix(12)
        let safeName = safeFilename(camera.name)
        let name: String
        if fullDay || daily {
            name = "timelapse_\(safeName)_\(filenameDay(start))_\(filenameDay(end))_\(speed)_\(digest).mp4"
        } else {
            name = "timelapse_\(safeName)_\(filenameDay(start))_\(filenameTime(start))_\(filenameDay(end))_\(filenameTime(end))_\(speed)__\(digest).mp4"
        }
        return outputDirectory.appendingPathComponent(name)
    }

    private static func isValidExport(_ url: URL) -> Bool {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = attributes[.size] as? NSNumber else { return false }
        return size.int64Value > 0
    }

    private func appendLog(level: String, message: String) {
        guard !message.isEmpty else { return }
        let timestamp = Self.logTimeFormatter.string(from: Date())
        for line in message.components(separatedBy: .newlines) where !line.isEmpty {
            logs.append(LogEntry(line: "\(timestamp) \(level.uppercased()) \(line)"))
        }
        if logs.count > 2_000 {
            logs.removeFirst(logs.count - 2_000)
        }
    }

    private func sendDownloadNotification(title: String, message: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = message
        content.sound = .default
        let request = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil
        )
        Task { @MainActor [weak self] in
            guard let self else { return }
            let notificationCenter = UNUserNotificationCenter.current()
            let settings = await notificationCenter.notificationSettings()
            guard settings.authorizationStatus == .authorized
                    || settings.authorizationStatus == .provisional,
                  settings.alertSetting == .enabled else {
                reportNotificationIssue(
                    "Enable notifications and banners for TimeLapse in System Settings → Notifications so download results can be shown."
                )
                NSApplication.shared.requestUserAttention(.informationalRequest)
                return
            }
            do {
                try await notificationCenter.add(request)
            } catch {
                reportNotificationIssue("Could not send the download notification: \(error.localizedDescription)")
            }
        }
    }

    private func showError(title: String, message: String) {
        statusMessage = title
        alert = AppAlert(title: title, message: message)
        appendLog(level: "ERROR", message: message)
    }

    private func finishShutdownIfReady() {
        guard isShuttingDown,
              cameraProcess == nil,
              downloadProcesses.isEmpty,
              thumbnailProcesses.isEmpty,
              let completion = shutdownCompletion else {
            return
        }
        shutdownCompletion = nil
        completion()
    }

    private static func iso8601String(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        formatter.timeZone = .current
        return formatter.string(from: date)
    }

    private static func filenameDay(_ date: Date) -> String {
        filenameDayFormatter.string(from: date)
    }

    private static func filenameTime(_ date: Date) -> String {
        filenameTimeFormatter.string(from: date)
    }

    private static func calendarDay(_ date: Date) -> String {
        calendarDayFormatter.string(from: date)
    }

    private static func safeFilename(_ value: String) -> String {
        let invalid = CharacterSet(charactersIn: "<>:\"/\\|?*")
            .union(.controlCharacters)
            .union(.whitespacesAndNewlines)
        let sanitized = value.components(separatedBy: invalid).filter { !$0.isEmpty }.joined(separator: "_")
            .trimmingCharacters(in: CharacterSet(charactersIn: "._-"))
        let limited = String(sanitized.prefix(48)).trimmingCharacters(in: CharacterSet(charactersIn: "._-"))
        return limited.isEmpty ? "camera" : limited
    }

    private static let filenameDayFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyy_MM_dd"
        return formatter
    }()

    private static let filenameTimeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        formatter.dateFormat = "HH_mm_ss"
        return formatter
    }()

    private static let logTimeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()

    private static let calendarDayFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()
}
