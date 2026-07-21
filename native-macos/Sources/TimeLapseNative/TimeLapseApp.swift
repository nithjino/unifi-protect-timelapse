import AppKit
import SwiftUI
import UserNotifications

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let model = AppModel()
    private var terminationPending = false

    func applicationWillFinishLaunching(_ notification: Notification) {
        let notificationCenter = UNUserNotificationCenter.current()
        notificationCenter.delegate = self
        guard
            let iconURL = Bundle.main.url(forResource: "TimeLapse", withExtension: "icns"),
            let icon = NSImage(contentsOf: iconURL)
        else {
            return
        }
        NSApplication.shared.applicationIconImage = icon
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            let notificationCenter = UNUserNotificationCenter.current()
            let settings = await notificationCenter.notificationSettings()
            switch settings.authorizationStatus {
            case .notDetermined:
                do {
                    let granted = try await notificationCenter.requestAuthorization(options: [.alert, .sound])
                    if !granted {
                        model.reportNotificationIssue(Self.notificationsDisabledMessage)
                    }
                } catch {
                    model.reportNotificationIssue("macOS could not request notification permission: \(error.localizedDescription)")
                }
            case .denied:
                model.reportNotificationIssue(Self.notificationsDisabledMessage)
            case .authorized, .provisional:
                if settings.alertSetting == .disabled {
                    model.reportNotificationIssue(Self.notificationsDisabledMessage)
                }
            @unknown default:
                model.reportNotificationIssue("macOS returned an unknown notification authorization state.")
            }
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !terminationPending else { return .terminateLater }
        if model.hasActiveDownloadJobs {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Download Jobs Are Still Running"
            alert.informativeText = "One or more download jobs are still running. Quitting TimeLapse will interrupt them and remove partial download files."
            alert.addButton(withTitle: "Quit and Interrupt")
            alert.addButton(withTitle: "Keep Running")
            guard alert.runModal() == .alertFirstButtonReturn else { return .terminateCancel }
        }
        guard model.hasActiveBackendProcesses else { return .terminateNow }
        terminationPending = true
        model.shutdown {
            DispatchQueue.main.async {
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }

    private static let notificationsDisabledMessage =
        "Enable notifications and banners for TimeLapse in System Settings → Notifications so download results can be shown."
}

@main
struct TimeLapseApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        Window("UniFi Protect Timelapse", id: "main") {
            MainView(model: appDelegate.model)
                .frame(minWidth: 1_250, minHeight: 560)
        }
        .defaultSize(width: 1_400, height: 680)
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Connection Profile…") {
                    appDelegate.model.presentNewProfile()
                }
                .keyboardShortcut("n")
                Button("Edit Connection Profile…") {
                    appDelegate.model.presentConnectionSettings()
                }
                .disabled(appDelegate.model.selectedProfile == nil)
            }
        }

        Window("Application Logs", id: "logs") {
            LogsDrawer(model: appDelegate.model)
                .padding(12)
                .frame(minWidth: 560, minHeight: 280)
        }
        .defaultSize(width: 760, height: 420)
        .windowResizability(.contentMinSize)
    }
}
