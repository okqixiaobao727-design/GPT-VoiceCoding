import Foundation
import Testing

@testable import ShellCore

@Suite struct EngineLocationTests {
    /// A scratch configuration file, written and cleaned up.
    private func withConfig(_ contents: String, _ body: (String) throws -> Void) rethrows {
        let directory = URL(fileURLWithPath: "/tmp/gvc-shell-config-\(UUID().uuidString.prefix(8))")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let file = directory.appendingPathComponent("config.toml")
        try? Data(contents.utf8).write(to: file)
        defer { try? FileManager.default.removeItem(at: directory) }
        try body(file.path)
    }

    @Test func theSocketPathIsReadFromTheConfigurationItSpawnsWith() throws {
        try withConfig(
            """
            # the engine's own file
            [engine]
            socket_path = "/tmp/gvc-elsewhere/control.sock"
            state_path  = "~/Library/Application Support/GPT-VoiceCoding/engine/state.json"

            [adapters]
            call = "somewhere:build"
            """
        ) { path in
            let located = try EngineLocation.resolve(configPath: path)
            #expect(located.socketPath == "/tmp/gvc-elsewhere/control.sock")
            #expect(located.configPath == path)
        }
    }

    @Test func theTelegramTokenVariableIsReadFromItsDocumentedTable() throws {
        let config = """
            [adapters.settings.companion_channel]
            token_env = "A_TELEGRAM_TOKEN"
            chat_id = "123"
            """

        let variable = try MinimalTOML.string(
            forKey: "token_env", inTable: "adapters.settings.companion_channel", of: config)

        #expect(variable == "A_TELEGRAM_TOKEN")
    }

    @Test func anAbsentKeyFallsBackToTheDocumentedDefault() throws {
        try withConfig("[engine]\nstate_path = \"/tmp/state.json\"\n") { path in
            let located = try EngineLocation.resolve(configPath: path)
            #expect(located.socketPath == EngineLocation.defaultSocketPath())
        }
    }

    @Test func anAbsentEngineTableFallsBackToo() throws {
        try withConfig("[adapters]\ncall = \"somewhere:build\"\n") { path in
            let located = try EngineLocation.resolve(configPath: path)
            #expect(located.socketPath == EngineLocation.defaultSocketPath())
        }
    }

    @Test func anAbsentFileIsNotAFailure() throws {
        // The engine refuses to start without one and says so on stderr; the
        // shell's job then is to show that, not to invent a second complaint.
        let located = try EngineLocation.resolve(configPath: "/tmp/gvc-nothing-here.toml")
        #expect(located.socketPath == EngineLocation.defaultSocketPath())
    }

    @Test func theDefaultIsPerUidAndShortEnoughToBind() {
        let path = EngineLocation.defaultSocketPath()
        #expect(path == "/tmp/gpt-voicecoding-\(getuid())/control.sock")
        #expect(path.utf8.count <= SocketOwnership.maxSocketPathBytes)
    }

    @Test func aTildeIsExpandedTheWayTheEngineExpandsIt() throws {
        try withConfig("[engine]\nsocket_path = \"~/sock\"\n") { path in
            let located = try EngineLocation.resolve(configPath: path)
            #expect(located.socketPath == NSHomeDirectory() + "/sock")
        }
    }

    @Test func aSocketPathThatIsNotAPathIsRefusedRatherThanIgnored() throws {
        // A misspelled setting that silently falls back to a default is the
        // configuration-shaped version of the silent fallback this repository bans.
        try withConfig("[engine]\nsocket_path = 7\n") { path in
            #expect(throws: ConfigurationFailure.self) {
                try EngineLocation.resolve(configPath: path)
            }
        }
        try withConfig("[engine]\nsocket_path = \"  \"\n") { path in
            #expect(throws: ConfigurationFailure.self) {
                try EngineLocation.resolve(configPath: path)
            }
        }
    }

    @Test func aKeyInAnotherTableIsNotTheEnginesKey() throws {
        try withConfig(
            """
            [adapters.settings.call]
            socket_path = "/tmp/not-the-engines.sock"
            """
        ) { path in
            let located = try EngineLocation.resolve(configPath: path)
            #expect(located.socketPath == EngineLocation.defaultSocketPath())
        }
    }

    @Test func aCommentedOutKeyIsNotAKey() throws {
        try withConfig("[engine]\n# socket_path = \"/tmp/commented.sock\"\n") { path in
            let located = try EngineLocation.resolve(configPath: path)
            #expect(located.socketPath == EngineLocation.defaultSocketPath())
        }
    }

    @Test func aTrailingCommentIsNotPartOfTheValue() throws {
        try withConfig("[engine]\nsocket_path = \"/tmp/a.sock\"  # short, per-uid\n") { path in
            let located = try EngineLocation.resolve(configPath: path)
            #expect(located.socketPath == "/tmp/a.sock")
        }
    }

    @Test func theEscapesRealTOMLAllowsAreRead() throws {
        // `tomllib` accepts these, so the engine would bind the escaped path
        // while a shell that refused it dialled the default — and the dropdown
        // would call a healthy engine unreachable for as long as it ran.
        // Built from pieces so the backslash in the file is unmistakable: the
        // written line is `socket_path = "/tmp/gvc-esc.sock"`, where
        // `/` is a solidus.
        let slash = "\\" + "u002F"
        try withConfig("[engine]\nsocket_path = \"\(slash)tmp\(slash)gvc-esc.sock\"\n") { path in
            let located = try EngineLocation.resolve(configPath: path)
            #expect(located.socketPath == "/tmp/gvc-esc.sock")
        }
    }

    @Test func aTruncatedEscapeIsRefusedRatherThanGuessedAt() throws {
        let truncated = "\\" + "u00"
        try withConfig("[engine]\nsocket_path = \"\(truncated)\"\n") { path in
            #expect(throws: ConfigurationFailure.self) {
                try EngineLocation.resolve(configPath: path)
            }
        }
    }

    @Test func aLiteralStringIsReadWithoutUnescaping() throws {
        try withConfig("[engine]\nsocket_path = '/tmp/b.sock'\n") { path in
            let located = try EngineLocation.resolve(configPath: path)
            #expect(located.socketPath == "/tmp/b.sock")
        }
    }
}
