import AppKit
import SwiftUI
import XCTest
@testable import TimeLapseNative

@MainActor
final class ConnectionSettingsViewTests: XCTestCase {
    func testLongPlainTextCredentialsDoNotReflowTheForm() {
        var settings = ConnectionSettings(
            instanceURL: "https://protect.local/proxy/protect/integration/v1",
            token: String(repeating: "t", count: 96),
            username: "timelapse",
            password: String(repeating: "p", count: 96)
        )
        let shortHeight = fittingHeight(for: settings)

        settings.instanceURL = "https://protect.local/" + String(repeating: "very-long-path/", count: 20)
        settings.username = String(repeating: "timelapse", count: 30)
        let longHeight = fittingHeight(for: settings)

        XCTAssertEqual(longHeight, shortHeight, accuracy: 1)
    }

    private func fittingHeight(for settings: ConnectionSettings) -> CGFloat {
        let view = ConnectionSettingsView(
            profileName: "Home",
            settings: settings,
            firstRun: false,
            isNewProfile: false,
            onSave: { _, _ in nil },
            onCancel: {}
        )
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(x: 0, y: 0, width: 600, height: 800)
        host.layoutSubtreeIfNeeded()
        return host.fittingSize.height
    }
}
