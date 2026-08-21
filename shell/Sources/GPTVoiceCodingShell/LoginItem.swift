import Foundation
import Observation
import ServiceManagement

/// Launch at login, as the app itself.
///
/// `SMAppService.mainApp` — **not** `agent(plistName:)`, which would put a
/// launchd job back in the picture and recreate the shape ADR 0005 moved away
/// from. The app is `LSUIElement`, so launching it at login means a menu-bar
/// item and no Dock icon, not a window.
@MainActor
@Observable
final class LoginItem {
    private(set) var enabled: Bool
    /// The system's own words when it refuses. Not rephrased here.
    private(set) var failure: String?

    init() {
        enabled = SMAppService.mainApp.status == .enabled
    }

    func set(_ wanted: Bool) {
        do {
            if wanted {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            failure = nil
        } catch {
            failure = error.localizedDescription
        }
        enabled = SMAppService.mainApp.status == .enabled
    }
}
