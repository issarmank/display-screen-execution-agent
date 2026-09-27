import AppKit
import ScreenAgentCore
import SwiftUI

@main
struct ScreenAgentApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        Window("Screen Agent", id: "conversation") {
            ConversationView(model: appDelegate.model)
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = AppEnvironment.makeViewModel()

    func applicationDidFinishLaunching(_ notification: Notification) {
        // A bare binary (`swift run`) isn't a bundle, so it needs this to get a Dock icon
        // and a focused window.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // Close the backend session before exiting, but never hang the quit on it.
        Task { @MainActor in
            await withTaskGroup(of: Void.self) { group in
                group.addTask { await self.model.endSession() }
                group.addTask { try? await Task.sleep(for: .seconds(2)) }
                await group.next()
                group.cancelAll()
            }
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}
