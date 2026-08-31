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
    @NSApplicationDelegateAdaptor(ShellDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            ControlPanelView(shell: delegate.shell)
        } label: {
            Image(systemName: delegate.shell.symbol)
        }
        .menuBarExtraStyle(.window)
    }
}

/// AppKit must give the shell a chance to stop the child before it goes away.
///
/// Adapted from `legacy@1d32845:bridge/daemon.py:3056-3065,3091-3101`: its owner
/// installed termination handlers before serving; delegate ownership keeps that order.
@MainActor
final class ShellDelegate: NSObject, NSApplicationDelegate {
    let shell: ShellModel
    override init() { shell = ShellModel() }
    init(shell: ShellModel) { self.shell = shell }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !shell.stopping else { return .terminateNow }
        Task {
            await shell.stopEngine()
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}
