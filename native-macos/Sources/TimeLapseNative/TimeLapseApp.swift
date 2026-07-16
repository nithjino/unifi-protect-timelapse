import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = AppModel()
    private var terminationPending = false

    func applicationWillFinishLaunching(_ notification: Notification) {
        guard
            let iconURL = Bundle.main.url(forResource: "TimeLapse", withExtension: "icns"),
            let icon = NSImage(contentsOf: iconURL)
        else {
            return
        }
        NSApplication.shared.applicationIconImage = icon
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard model.hasActiveBackendProcesses else { return .terminateNow }
        guard !terminationPending else { return .terminateLater }
        terminationPending = true
        model.shutdown {
            DispatchQueue.main.async {
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }
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
