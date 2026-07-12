import Foundation
import XCTest
@testable import TimeLapseNative

final class CredentialStoreTests: XCTestCase {
    func testKeychainSaveUpdateAndLoad() throws {
        let store = testKeychain()
        defer { try? store.delete() }
        var settings = completeSettings

        try store.save(settings)
        XCTAssertEqual(try store.load(), settings)

        settings.password = "replacement password"
        settings.token = "replacement token"
        try store.save(settings)
        XCTAssertEqual(try store.load(), settings)
    }

    func testLegacyEnvironmentMigratesToKeychainAndIsRemoved() throws {
        let store = testKeychain()
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let legacyURL = directory.appendingPathComponent(".env")
        defer {
            try? store.delete()
            try? FileManager.default.removeItem(at: directory)
        }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try """
        UNIFI_PROTECT_URL="https://protect.local/proxy/protect/integration/v1"
        UNIFI_PROTECT_TOKEN="token with spaces"
        UNIFI_PROTECT_USERNAME="camera user"
        UNIFI_PROTECT_PASSWORD="secret\\\\value"
        UNIFI_PROTECT_VERIFY_SSL=false
        TIMELAPSE_REQUEST_TIMEOUT_SECONDS=0
        TIMELAPSE_MAX_DOWNLOAD_MIB=10240
        """.write(to: legacyURL, atomically: true, encoding: .utf8)

        let result = try CredentialStore.load(keychain: store, legacyURL: legacyURL)

        XCTAssertTrue(result.exists)
        XCTAssertTrue(result.didRemoveLegacyPlaintext)
        XCTAssertNil(result.warning)
        XCTAssertFalse(FileManager.default.fileExists(atPath: legacyURL.path))
        XCTAssertEqual(try store.load(), result.settings)
        XCTAssertEqual(result.settings.password, "secret\\value")
        XCTAssertFalse(result.settings.verifySSL)
    }

    func testConnectionRequiresAllFieldsAndSafeHTTPSURL() {
        var settings = ConnectionSettings(
            instanceURL: "http://protect.local/proxy/protect/integration/v1",
            token: "token",
            username: "user",
            password: "password"
        )
        XCTAssertNotNil(settings.validationError)
        settings.instanceURL = "https://protect.local/proxy/protect/integration/v1/"
        XCTAssertNil(settings.validationError)
        XCTAssertEqual(settings.normalized.instanceURL, "https://protect.local/proxy/protect/integration/v1")

        settings.instanceURL = "https://user:password@protect.local/proxy/protect/integration/v1"
        XCTAssertEqual(
            settings.validationError,
            "The Protect URL must not contain a username or password. Use the dedicated credential fields instead."
        )
        settings.instanceURL = "https://protect.local/proxy/protect/integration/v1?token=value"
        XCTAssertEqual(settings.validationError, "The Protect URL must not contain a query string or fragment.")
    }

    func testProfileNameDefaultsToNormalizedProtectURL() {
        var settings = completeSettings
        settings.instanceURL += "/"
        let profile = ConnectionProfile(name: "   ", settings: settings).normalized

        XCTAssertEqual(profile.name, "https://protect.local/proxy/protect/integration/v1")
        XCTAssertEqual(profile.displayName, profile.settings.instanceURL)
    }

    func testLegacyKeychainConnectionMigratesToProfileCollection() throws {
        let credentialStore = testKeychain()
        let profileStore = KeychainProfileStore(
            service: "io.timelapse.desktop.profile-tests.\(UUID().uuidString)",
            account: "test"
        )
        let missingLegacyURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathComponent(".env")
        defer {
            try? credentialStore.delete()
            try? profileStore.delete()
        }
        try credentialStore.save(completeSettings)

        let result = try ProfileStore.load(
            keychain: profileStore,
            legacyKeychain: credentialStore,
            legacyURL: missingLegacyURL
        )

        XCTAssertEqual(result.state.profiles.count, 1)
        XCTAssertEqual(result.state.profiles.first?.settings, completeSettings)
        XCTAssertEqual(result.state.selectedProfileID, result.state.profiles.first?.id)
        XCTAssertNotNil(result.migrationMessage)
        XCTAssertNil(try credentialStore.load())
        XCTAssertEqual(try profileStore.load(), result.state)
    }

    private var completeSettings: ConnectionSettings {
        ConnectionSettings(
            instanceURL: "https://protect.local/proxy/protect/integration/v1",
            token: "token with spaces and \"quotes\"",
            username: "camera user",
            password: "secret\\value"
        )
    }

    private func testKeychain() -> KeychainCredentialStore {
        KeychainCredentialStore(
            service: "io.timelapse.desktop.tests.\(UUID().uuidString)",
            account: "test"
        )
    }
}
