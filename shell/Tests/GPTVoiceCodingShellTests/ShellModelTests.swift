import Foundation
import ShellCore
import Testing

@testable import GPTVoiceCodingShell

@MainActor
@Suite struct ShellModelTests {
    @Test func aMissingNamedVariableStopsAtPreflight() throws {
        let directory = URL(
            fileURLWithPath: "/tmp/gvc-shell-preflight-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let config = directory.appendingPathComponent("config.toml")
        try Data(
            """
            [adapters.settings.companion_channel]
            token_env = "A_TELEGRAM_TOKEN"
            """.utf8
        ).write(to: config)
        let credentials = TelegramCredentials(
            configPath: config.path,
            environmentPath: directory.appendingPathComponent("environment").path)

        let state = ShellModel.preflight(credentials: credentials)

        #expect(state == .missing)
    }
}
