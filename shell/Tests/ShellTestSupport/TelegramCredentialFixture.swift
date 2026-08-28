import Darwin
import Foundation
import ShellCore

public final class TelegramCredentialFixture {
    public let directory: URL
    public let configPath: String
    public let environmentPath: String

    public init(tokenVariable: String = "A_TELEGRAM_TOKEN") throws {
        directory = URL(
            fileURLWithPath: "/tmp/gvc-telegram-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let config = directory.appendingPathComponent("config.toml")
        try Data(
            """
            [adapters.settings.companion_channel]
            token_env = "\(tokenVariable)"
            chat_id = "123"
            """.utf8
        ).write(to: config)
        configPath = config.path
        environmentPath = directory.appendingPathComponent("environment").path
    }

    deinit {
        try? FileManager.default.removeItem(at: directory)
    }

    public var credentials: TelegramCredentials {
        TelegramCredentials(configPath: configPath, environmentPath: environmentPath)
    }

    public func writeEnvironment(_ contents: String, mode: mode_t = 0o600) throws {
        try Data(contents.utf8).write(to: URL(fileURLWithPath: environmentPath))
        guard chmod(environmentPath, mode) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
    }
}
