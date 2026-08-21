import Foundation
import Testing

@testable import ShellCore

@Suite struct EngineCommandTests {
    /// A directory holding an executable file by the given name.
    private func withExecutable(
        _ names: [String], _ body: (URL) throws -> Void
    ) rethrows {
        let directory = URL(fileURLWithPath: "/tmp/gvc-shell-bin-\(UUID().uuidString.prefix(8))")
        for name in names {
            let file = directory.appendingPathComponent(name)
            try? FileManager.default.createDirectory(
                at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            FileManager.default.createFile(
                atPath: file.path, contents: Data("#!/bin/sh\n".utf8),
                attributes: [.posixPermissions: 0o755])
        }
        defer { try? FileManager.default.removeItem(at: directory) }
        try body(directory)
    }

    @Test func theBundledInterpreterWinsWhenItIsThere() throws {
        try withExecutable([BundleLayout.engineInterpreterRelativePath]) { resources in
            let command = try EngineCommand.resolve(
                resources: resources, configPath: "/tmp/config.toml", environment: [:],
                searchPath: [])
            #expect(
                command.executable
                    == resources.appendingPathComponent(
                        BundleLayout.engineInterpreterRelativePath
                    ).path)
            #expect(
                command.arguments == [
                    "-m", "gpt_voicecoding.engine", "--config", "/tmp/config.toml",
                ])
            #expect(command.source == .bundled)
        }
    }

    @Test func theDeveloperPathIsAStatedFeatureNotAStopgap() throws {
        // Headless mode stays real: the engine must keep running standalone
        // without the shell, and the shell must be able to drive a checkout.
        try withExecutable(["python3"]) { bin in
            let command = try EngineCommand.resolve(
                resources: nil, configPath: "/tmp/config.toml", environment: [:],
                searchPath: [bin.path])
            #expect(command.executable == bin.appendingPathComponent("python3").path)
            #expect(command.source == .developerPath)
        }
    }

    @Test func aNamedInterpreterOverridesTheSearchPath() throws {
        try withExecutable(["my-python"]) { bin in
            let named = bin.appendingPathComponent("my-python").path
            let command = try EngineCommand.resolve(
                resources: nil, configPath: "/tmp/config.toml",
                environment: [EngineCommand.interpreterVariable: named],
                searchPath: ["/nowhere"])
            #expect(command.executable == named)
            #expect(command.source == .named)
        }
    }

    @Test func aNamedInterpreterThatIsNotThereIsARefusalNotAFallback() throws {
        try withExecutable(["python3"]) { bin in
            #expect(throws: EngineCommandFailure.self) {
                try EngineCommand.resolve(
                    resources: nil, configPath: "/tmp/config.toml",
                    environment: [EngineCommand.interpreterVariable: "/tmp/gvc-no-such-python"],
                    searchPath: [bin.path])
            }
        }
    }

    @Test func noInterpreterAnywhereIsSaidOutLoud() {
        #expect(throws: EngineCommandFailure.self) {
            try EngineCommand.resolve(
                resources: nil, configPath: "/tmp/config.toml", environment: [:],
                searchPath: ["/tmp/gvc-empty-\(UUID().uuidString.prefix(8))"])
        }
    }

    @Test func aNonExecutableFileIsNotAnInterpreter() throws {
        let directory = URL(fileURLWithPath: "/tmp/gvc-shell-bin-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        FileManager.default.createFile(
            atPath: directory.appendingPathComponent("python3").path, contents: Data(),
            attributes: [.posixPermissions: 0o644])
        defer { try? FileManager.default.removeItem(at: directory) }

        #expect(throws: EngineCommandFailure.self) {
            try EngineCommand.resolve(
                resources: nil, configPath: "/tmp/config.toml", environment: [:],
                searchPath: [directory.path])
        }
    }

    @Test func theBundledNameLivesInExactlyOnePlace() {
        // The engine binary's name inside the bundle is #12's decision. It is a
        // constant here so #12 has one line to change, not a search to run.
        #expect(BundleLayout.engineInterpreterRelativePath.contains("engine/"))
    }
}
