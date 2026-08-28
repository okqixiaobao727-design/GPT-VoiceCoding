import Darwin
import Foundation
import Testing

@testable import ShellCore

@Suite struct TelegramCredentialsTests {
    @Test func aFileOpenToGroupOrOthersIsRefused() throws {
        for mode: mode_t in [0o601, 0o610, 0o604, 0o644] {
            try withFiles(environment: "A_TELEGRAM_TOKEN=123:abc\n", mode: mode) { config, file in
                let reading = TelegramCredentials(configPath: config, environmentPath: file).load()

                guard case .unsafe(let detail) = reading.state else {
                    Issue.record("expected mode \(String(mode, radix: 8)) to be unsafe")
                    return
                }
                #expect(detail.contains("0600"))
                #expect(reading.environment.isEmpty)
            }
        }
    }

    @Test func aPrivateFileSuppliesEveryLiteralEnvironmentValue() throws {
        try withFiles(
            environment: """
                # Hand-editable, but never shell-expanded.

                A_TELEGRAM_TOKEN=123:abc=still-the-value
                ANOTHER_SETTING=literal $HOME
                """,
            mode: 0o600
        ) { config, file in
            let reading = TelegramCredentials(configPath: config, environmentPath: file).load()

            #expect(reading.state == .ready)
            #expect(
                reading.environment == [
                    "A_TELEGRAM_TOKEN": "123:abc=still-the-value",
                    "ANOTHER_SETTING": "literal $HOME",
                ])
        }
    }

    @Test func savingCreatesAPrivateFileUnderTheConfiguredVariableName() throws {
        try withFiles(environment: nil, mode: 0o600) { config, file in
            let credentials = TelegramCredentials(configPath: config, environmentPath: file)

            let reading = try credentials.save(token: "fresh=token")

            #expect(reading.state == .ready)
            #expect(reading.environment == ["A_TELEGRAM_TOKEN": "fresh=token"])
            let attributes = try FileManager.default.attributesOfItem(atPath: file)
            #expect(attributes[.posixPermissions] as? Int == 0o600)
            #expect(
                try String(contentsOfFile: file, encoding: .utf8)
                    == "A_TELEGRAM_TOKEN=fresh=token\n")
        }
    }

    @Test func aMissingOrChannelFreeConfigurationDoesNotPreemptTheEngine() throws {
        let directory = URL(fileURLWithPath: "/tmp/gvc-telegram-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let environment = directory.appendingPathComponent("environment").path
        let missing = TelegramCredentials(
            configPath: directory.appendingPathComponent("missing.toml").path,
            environmentPath: environment)
        #expect(missing.load().state == .notConfigured)

        let channelFree = directory.appendingPathComponent("config.toml")
        try Data(
            """
            [adapters]
            enabled = ["null_channel"]
            """.utf8
        ).write(to: channelFree)
        let notConfigured = TelegramCredentials(
            configPath: channelFree.path, environmentPath: environment)

        #expect(notConfigured.load().state == .notConfigured)
        #expect(notConfigured.load().environment.isEmpty)
    }

    @Test func savingPreservesTheHandEditedFileAroundTheToken() throws {
        let original = """
            # Companion environment

            Z_SETTING=last
            A_TELEGRAM_TOKEN=old
            A_SETTING=first
            """
        try withFiles(environment: original, mode: 0o600) { config, file in
            let credentials = TelegramCredentials(configPath: config, environmentPath: file)

            _ = try credentials.save(token: "new")

            #expect(
                try String(contentsOfFile: file, encoding: .utf8)
                    == """
                    # Companion environment

                    Z_SETTING=last
                    A_TELEGRAM_TOKEN=new
                    A_SETTING=first
                    """)
        }
    }

    @Test func aMalformedOrDuplicateLineRefusesTheWholeFile() throws {
        for text in [
            "A_TELEGRAM_TOKEN=one\nA_TELEGRAM_TOKEN=two\n",
            "1_INVALID=value\nA_TELEGRAM_TOKEN=one\n",
            "NOT_AN_ASSIGNMENT\nA_TELEGRAM_TOKEN=one\n",
        ] {
            try withFiles(environment: text, mode: 0o600) { config, file in
                let reading = TelegramCredentials(configPath: config, environmentPath: file).load()

                guard case .unreadable = reading.state else {
                    Issue.record("expected the whole malformed file to be unreadable")
                    return
                }
                #expect(reading.environment.isEmpty)
            }
        }
    }

    @Test func aPrivateFileWithoutTheNamedVariableIsMissing() throws {
        try withFiles(environment: "ANOTHER_SETTING=kept\n", mode: 0o600) { config, file in
            let reading = TelegramCredentials(configPath: config, environmentPath: file).load()

            #expect(reading.state == .missing)
            #expect(reading.environment == ["ANOTHER_SETTING": "kept"])
        }
    }

    @Test func savingPreservesOtherVariablesAndRefusesAnEmptyToken() throws {
        try withFiles(
            environment: "ANOTHER_SETTING=kept\nA_TELEGRAM_TOKEN=old\n", mode: 0o600
        ) { config, file in
            let credentials = TelegramCredentials(configPath: config, environmentPath: file)
            _ = try credentials.save(token: "new")

            #expect(
                credentials.load().environment == [
                    "ANOTHER_SETTING": "kept", "A_TELEGRAM_TOKEN": "new",
                ])
            #expect(throws: TelegramCredentialSaveFailure.self) {
                try credentials.save(token: "   ")
            }
            #expect(credentials.load().environment["A_TELEGRAM_TOKEN"] == "new")
        }
    }

    @Test func theDefaultFileIsBesideTheDefaultConfiguration() {
        let credentials = TelegramCredentials()

        #expect(
            credentials.environmentPath
                == NSHomeDirectory()
                + "/Library/Application Support/GPT-VoiceCoding/engine/environment")
    }

    private func withFiles(
        environment: String?,
        mode: mode_t,
        _ body: (String, String) throws -> Void
    ) throws {
        let directory = URL(fileURLWithPath: "/tmp/gvc-telegram-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let config = directory.appendingPathComponent("config.toml")
        try Data(
            """
            [adapters.settings.companion_channel]
            token_env = "A_TELEGRAM_TOKEN"
            chat_id = "123"
            """.utf8
        ).write(to: config)
        let file = directory.appendingPathComponent("environment")
        if let environment {
            try Data(environment.utf8).write(to: file)
            #expect(chmod(file.path, mode) == 0)
        }

        try body(config.path, file.path)
    }
}
