import Foundation

struct ConnectionSettings: Codable, Equatable, Sendable {
    var instanceURL = ""
    var token = ""
    var username = ""
    var password = ""
    var verifySSL = true
    var requestTimeoutSeconds = 0
    var maxDownloadMiB = 10 * 1024

    var missingFieldNames: [String] {
        [
            ("Protect URL", instanceURL),
            ("API token", token),
            ("local username", username),
            ("local password", password),
        ].compactMap { name, value in value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? name : nil }
    }

    var normalized: ConnectionSettings {
        var copy = self
        copy.instanceURL = instanceURL.trimmingCharacters(in: .whitespacesAndNewlines)
        while copy.instanceURL.hasSuffix("/") {
            copy.instanceURL.removeLast()
        }
        copy.token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        copy.username = username.trimmingCharacters(in: .whitespacesAndNewlines)
        return copy
    }

    var validationError: String? {
        let candidate = normalized
        if !candidate.missingFieldNames.isEmpty {
            return "Please provide: \(candidate.missingFieldNames.joined(separator: ", "))."
        }
        guard
            let components = URLComponents(string: candidate.instanceURL),
            components.scheme?.lowercased() == "https",
            components.host?.isEmpty == false
        else {
            return "The Protect URL must be a complete HTTPS URL, such as https://protect.local/proxy/protect/integration/v1."
        }
        if components.user != nil || components.password != nil {
            return "The Protect URL must not contain a username or password. Use the dedicated credential fields instead."
        }
        if components.query != nil || components.fragment != nil {
            return "The Protect URL must not contain a query string or fragment."
        }
        return nil
    }
}

struct ConnectionProfile: Codable, Equatable, Identifiable, Sendable {
    let id: UUID
    var name: String
    var settings: ConnectionSettings

    init(id: UUID = UUID(), name: String = "", settings: ConnectionSettings = ConnectionSettings()) {
        self.id = id
        self.name = name
        self.settings = settings
    }

    var displayName: String {
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmedName.isEmpty ? settings.normalized.instanceURL : trimmedName
    }

    var normalized: ConnectionProfile {
        var copy = self
        copy.settings = settings.normalized
        copy.name = displayName
        return copy
    }
}

struct ConnectionProfileState: Codable, Equatable, Sendable {
    var profiles: [ConnectionProfile]
    var selectedProfileID: UUID?

    init(profiles: [ConnectionProfile] = [], selectedProfileID: UUID? = nil) {
        self.profiles = profiles
        self.selectedProfileID = selectedProfileID
    }

    var normalized: ConnectionProfileState {
        let normalizedProfiles = profiles.map(\.normalized)
        let validSelection = normalizedProfiles.contains { $0.id == selectedProfileID }
        return ConnectionProfileState(
            profiles: normalizedProfiles,
            selectedProfileID: validSelection ? selectedProfileID : normalizedProfiles.first?.id
        )
    }
}

struct BackendSettings: Encodable, Sendable {
    let instanceURL: String
    let token: String
    let username: String
    let password: String
    let verifySSL: Bool
    let requestTimeoutSeconds: Int
    let maxDownloadMiB: Int

    init(_ settings: ConnectionSettings) {
        let settings = settings.normalized
        instanceURL = settings.instanceURL
        token = settings.token
        username = settings.username
        password = settings.password
        verifySSL = settings.verifySSL
        requestTimeoutSeconds = settings.requestTimeoutSeconds
        maxDownloadMiB = settings.maxDownloadMiB
    }

    enum CodingKeys: String, CodingKey {
        case instanceURL = "instance_url"
        case token
        case username
        case password
        case verifySSL = "verify_ssl"
        case requestTimeoutSeconds = "request_timeout_seconds"
        case maxDownloadMiB = "max_download_mib"
    }
}

struct CameraInfo: Codable, Hashable, Identifiable, Sendable {
    let id: String
    let name: String
    let state: String?
    let model: String?
}

struct ListCamerasRequest: Encodable, Sendable {
    let id: String
    let command = "list_cameras"
    let settings: BackendSettings
}

struct DownloadRequest: Encodable, Sendable {
    let id: String
    let command = "download"
    let settings: BackendSettings
    let camera: CameraInfo
    let start: String
    let end: String
    let speed: String
    let output: String
}

struct BackendEvent: Decodable, Sendable {
    let id: String?
    let event: String
    let level: String?
    let message: String?
    let cameras: [CameraInfo]?
    let downloadedBytes: Int64?
    let totalBytes: Int64?
    let bytesPerSecond: Double?
    let elapsedSeconds: Double?
    let output: String?

    enum CodingKeys: String, CodingKey {
        case id
        case event
        case level
        case message
        case cameras
        case downloadedBytes = "downloaded_bytes"
        case totalBytes = "total_bytes"
        case bytesPerSecond = "bytes_per_second"
        case elapsedSeconds = "elapsed_seconds"
        case output
    }
}

enum DownloadState: Equatable, Sendable {
    case scheduled
    case preparing
    case downloading
    case cancelling
    case completed
    case cancelled
    case failed(String)
    case stopped

    var text: String {
        switch self {
        case .scheduled: "Scheduled daily"
        case .preparing: "Preparing export…"
        case .downloading: "Downloading"
        case .cancelling: "Cancelling…"
        case .completed: "Completed"
        case .cancelled: "Cancelled"
        case let .failed(message): "Failed: \(message)"
        case .stopped: "Stopped"
        }
    }

    var isTerminal: Bool {
        switch self {
        case .completed, .cancelled, .failed, .stopped: true
        case .scheduled, .preparing, .downloading, .cancelling: false
        }
    }

    var isFailure: Bool {
        if case .failed = self { return true }
        return false
    }
}

@MainActor
final class DownloadJob: ObservableObject, Identifiable {
    let id: UUID
    let groupNumber: Int
    let camera: CameraInfo
    let outputURL: URL
    let requestSettings: BackendSettings
    let requestStart: String
    let requestEnd: String
    let requestSpeed: String
    let isDailySchedule: Bool
    @Published var state: DownloadState
    @Published var downloadedBytes: Int64 = 0
    @Published var totalBytes: Int64?
    @Published var bytesPerSecond = 0.0
    @Published var elapsedSeconds = 0.0
    @Published var lastProgressAt: Date?

    init(
        id: UUID = UUID(),
        groupNumber: Int,
        camera: CameraInfo,
        outputURL: URL,
        requestSettings: BackendSettings,
        requestStart: String,
        requestEnd: String,
        requestSpeed: String,
        isDailySchedule: Bool = false,
        initialState: DownloadState = .preparing
    ) {
        self.id = id
        self.groupNumber = groupNumber
        self.camera = camera
        self.outputURL = outputURL
        self.requestSettings = requestSettings
        self.requestStart = requestStart
        self.requestEnd = requestEnd
        self.requestSpeed = requestSpeed
        self.isDailySchedule = isDailySchedule
        state = initialState
    }
}

struct LogEntry: Identifiable, Sendable {
    let id = UUID()
    let line: String
}

struct AppAlert: Identifiable {
    let id = UUID()
    let title: String
    let message: String
}
