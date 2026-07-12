import Foundation
import Security

enum CredentialStoreError: LocalizedError {
    case invalidKeychainData(Error)
    case keychainFailure(operation: String, status: OSStatus)
    case unexpectedKeychainValue

    var errorDescription: String? {
        switch self {
        case let .invalidKeychainData(error):
            "The Keychain item contains invalid application data: \(error.localizedDescription)"
        case let .keychainFailure(operation, status):
            "Keychain \(operation) failed: \(Self.message(for: status)) (\(status))."
        case .unexpectedKeychainValue:
            "Keychain returned application data in an unexpected format."
        }
    }

    private static func message(for status: OSStatus) -> String {
        SecCopyErrorMessageString(status, nil) as String? ?? "unknown Keychain error"
    }
}

struct KeychainJSONStore<Value: Codable & Sendable>: Sendable {
    let service: String
    let account: String

    func load() throws -> Value? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess else {
            throw CredentialStoreError.keychainFailure(operation: "read", status: status)
        }
        guard let data = result as? Data else {
            throw CredentialStoreError.unexpectedKeychainValue
        }
        do {
            return try JSONDecoder().decode(Value.self, from: data)
        } catch {
            throw CredentialStoreError.invalidKeychainData(error)
        }
    }

    func save(_ value: Value) throws {
        let data = try JSONEncoder().encode(value)
        let changes = [kSecValueData as String: data]
        var status = SecItemUpdate(baseQuery as CFDictionary, changes as CFDictionary)
        if status == errSecItemNotFound {
            var item = baseQuery
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            status = SecItemAdd(item as CFDictionary, nil)
        }
        guard status == errSecSuccess else {
            throw CredentialStoreError.keychainFailure(operation: "write", status: status)
        }
    }

    func delete() throws {
        let status = SecItemDelete(baseQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw CredentialStoreError.keychainFailure(operation: "delete", status: status)
        }
    }

    private var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}

typealias KeychainCredentialStore = KeychainJSONStore<ConnectionSettings>
typealias KeychainProfileStore = KeychainJSONStore<ConnectionProfileState>

struct CredentialLoadResult {
    let settings: ConnectionSettings
    let exists: Bool
    let didRemoveLegacyPlaintext: Bool
    let warning: String?

    init(
        settings: ConnectionSettings,
        exists: Bool,
        didRemoveLegacyPlaintext: Bool = false,
        warning: String? = nil
    ) {
        self.settings = settings
        self.exists = exists
        self.didRemoveLegacyPlaintext = didRemoveLegacyPlaintext
        self.warning = warning
    }
}

enum CredentialStore {
    static let liveKeychain = KeychainCredentialStore(
        service: "io.timelapse.desktop.protect-credentials",
        account: "default"
    )

    static var legacyDotenvURL: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent("TimeLapse/.env")
    }

    static func load(
        keychain: KeychainCredentialStore = liveKeychain,
        legacyURL: URL = legacyDotenvURL
    ) throws -> CredentialLoadResult {
        if let settings = try keychain.load() {
            let cleanup = removeLegacyPlaintext(at: legacyURL)
            return CredentialLoadResult(
                settings: settings,
                exists: true,
                didRemoveLegacyPlaintext: cleanup.removed,
                warning: cleanup.warning
            )
        }

        guard FileManager.default.fileExists(atPath: legacyURL.path) else {
            return CredentialLoadResult(settings: ConnectionSettings(), exists: false)
        }
        let settings = try LegacyEnvironmentStore.load(from: legacyURL)
        try keychain.save(settings)
        let cleanup = removeLegacyPlaintext(at: legacyURL)
        return CredentialLoadResult(
            settings: settings,
            exists: true,
            didRemoveLegacyPlaintext: cleanup.removed,
            warning: cleanup.warning
        )
    }

    static func save(_ settings: ConnectionSettings, keychain: KeychainCredentialStore = liveKeychain) throws {
        try keychain.save(settings.normalized)
    }

    private static func removeLegacyPlaintext(at url: URL) -> (removed: Bool, warning: String?) {
        guard FileManager.default.fileExists(atPath: url.path) else { return (false, nil) }
        do {
            try FileManager.default.removeItem(at: url)
            return (true, nil)
        } catch {
            let warning = (
                "Credentials are stored in the macOS Keychain, but the legacy plaintext file could not be removed "
                    + "from \(url.path): \(error.localizedDescription). Remove that file manually."
            )
            return (false, warning)
        }
    }
}

struct ProfileLoadResult {
    let state: ConnectionProfileState
    let migrationMessage: String?
    let warning: String?
}

enum ProfileStore {
    static let liveKeychain = KeychainProfileStore(
        service: "io.timelapse.desktop.connection-profiles",
        account: "default"
    )

    static func load(
        keychain: KeychainProfileStore = liveKeychain,
        legacyKeychain: KeychainCredentialStore = CredentialStore.liveKeychain,
        legacyURL: URL = CredentialStore.legacyDotenvURL
    ) throws -> ProfileLoadResult {
        if let state = try keychain.load() {
            return ProfileLoadResult(state: state.normalized, migrationMessage: nil, warning: nil)
        }

        let legacy = try CredentialStore.load(keychain: legacyKeychain, legacyURL: legacyURL)
        guard legacy.exists else {
            return ProfileLoadResult(state: ConnectionProfileState(), migrationMessage: nil, warning: legacy.warning)
        }

        let profile = ConnectionProfile(name: legacy.settings.normalized.instanceURL, settings: legacy.settings).normalized
        let state = ConnectionProfileState(profiles: [profile], selectedProfileID: profile.id)
        try keychain.save(state)
        var warning = legacy.warning
        do {
            try legacyKeychain.delete()
        } catch {
            let cleanupWarning = "The new profile was saved, but the old Keychain item could not be removed: \(error.localizedDescription)"
            warning = [warning, cleanupWarning].compactMap { $0 }.joined(separator: "\n\n")
        }
        let message = legacy.didRemoveLegacyPlaintext
            ? "Moved the existing Protect connection from plaintext into a Keychain-backed profile"
            : "Moved the existing Protect connection into a Keychain-backed profile"
        return ProfileLoadResult(state: state, migrationMessage: message, warning: warning)
    }

    static func save(
        _ state: ConnectionProfileState,
        keychain: KeychainProfileStore = liveKeychain
    ) throws {
        try keychain.save(state.normalized)
    }
}

enum LegacyEnvironmentStore {
    static func load(from url: URL) throws -> ConnectionSettings {
        let contents = try String(contentsOf: url, encoding: .utf8)
        let values = parse(contents)
        return ConnectionSettings(
            instanceURL: values["UNIFI_PROTECT_URL"] ?? "",
            token: values["UNIFI_PROTECT_TOKEN"] ?? "",
            username: values["UNIFI_PROTECT_USERNAME"] ?? "",
            password: values["UNIFI_PROTECT_PASSWORD"] ?? "",
            verifySSL: parseBoolean(values["UNIFI_PROTECT_VERIFY_SSL"], default: true),
            requestTimeoutSeconds: parseNonnegativeInteger(
                values["TIMELAPSE_REQUEST_TIMEOUT_SECONDS"],
                default: 0
            ),
            maxDownloadMiB: parseNonnegativeInteger(values["TIMELAPSE_MAX_DOWNLOAD_MIB"], default: 10 * 1024)
        )
    }

    static func parse(_ contents: String) -> [String: String] {
        var result: [String: String] = [:]
        for rawLine in contents.split(whereSeparator: \.isNewline) {
            var line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("export ") {
                line.removeFirst("export ".count)
            }
            guard !line.hasPrefix("#"), let equals = line.firstIndex(of: "=") else { continue }
            let key = String(line[..<equals]).trimmingCharacters(in: .whitespaces)
            let rawValue = String(line[line.index(after: equals)...]).trimmingCharacters(in: .whitespaces)
            guard !key.isEmpty else { continue }
            result[key] = unquote(rawValue)
        }
        return result
    }

    private static func unquote(_ value: String) -> String {
        guard value.count >= 2, value.first == "\"", value.last == "\"" else { return value }
        let body = value.dropFirst().dropLast()
        var output = ""
        var escaping = false
        for character in body {
            if escaping {
                switch character {
                case "n": output.append("\n")
                case "r": output.append("\r")
                default: output.append(character)
                }
                escaping = false
            } else if character == "\\" {
                escaping = true
            } else {
                output.append(character)
            }
        }
        if escaping { output.append("\\") }
        return output
    }

    private static func parseBoolean(_ value: String?, default defaultValue: Bool) -> Bool {
        switch value?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "true", "1", "yes", "y", "on": true
        case "false", "0", "no", "n", "off": false
        default: defaultValue
        }
    }

    private static func parseNonnegativeInteger(_ value: String?, default defaultValue: Int) -> Int {
        guard let value, let parsed = Int(value), parsed >= 0 else { return defaultValue }
        return parsed
    }
}
