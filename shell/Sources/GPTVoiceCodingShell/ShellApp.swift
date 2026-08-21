import AppKit
import ShellCore
import SwiftUI

/// The menu-bar shell.
///
/// `MenuBarExtra` in `.window` style rather than an `NSStatusItem` menu: the
/// honest failure panels are multi-line — the engine's own stderr, verbatim — and
/// an `NSMenu` renders those badly. What that costs is fine control over the
/// status item, which this shell does not need.
///
/// `LSUIElement` is 1 in the bundle's `Info.plist`: menu bar only, no Dock icon.
@main
struct ShellApp: App {
    @State private var shell = ShellModel()
    @NSApplicationDelegateAdaptor(ShellDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            ControlPanelView(shell: shell)
                .onAppear { delegate.shell = shell }
        } label: {
            Image(systemName: shell.symbol)
        }
        .menuBarExtraStyle(.window)
    }
}

/// The one thing AppKit has to be asked for: a chance to stop the child before
/// this process goes away.
///
/// Quitting must reach the engine, because `SIGTERM` is what makes it stop in
/// order — loops cancelled, adapters closed in reverse, socket removed. A shell
/// that vanished without asking would leave the next start claiming its own
/// debris.
@MainActor
final class ShellDelegate: NSObject, NSApplicationDelegate {
    var shell: ShellModel?

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let shell, !shell.stopping else { return .terminateNow }
        Task {
            await shell.stopEngine()
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}
