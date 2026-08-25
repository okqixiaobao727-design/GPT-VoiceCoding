import Foundation
import Testing

@testable import ShellCore

/// The shell's whole part in installation (ADR 0012): resolve one interpreter,
/// run one verb, report what it said. Everything the verb *does* is Python's, and
/// is tested there — a second copy of it here would be the duplication this
/// arrangement exists to avoid.
@Suite(.serialized) struct InstallationTests {
    private func withExecutable(_ names: [String], _ body: (URL) throws -> Void) rethrows {
        let directory = URL(fileURLWithPath: "/tmp/gvc-install-\(UUID().uuidString.prefix(8))")
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

    @Test func itRunsTheInstallationModuleOnTheBundledInterpreter() throws {
        try withExecutable([BundleLayout.engineInterpreterRelativePath]) { resources in
            let command = try EngineCommand.resolveInstallation(
                resources: resources, verb: Installation.reconcileVerb, environment: [:],
                searchPath: [])

            #expect(
                command.executable
                    == resources.appendingPathComponent(
                        BundleLayout.engineInterpreterRelativePath
                    ).path)
            #expect(command.arguments == ["-m", "gpt_voicecoding.installation", "reconcile"])
            #expect(command.source == .bundled)
        }
    }

    @Test func itCarriesNoConfigPath() throws {
        // The engine refuses to start without a config.toml the user writes by
        // hand. An installation that waited for that file would never happen.
        try withExecutable([BundleLayout.engineInterpreterRelativePath]) { resources in
            let command = try EngineCommand.resolveInstallation(
                resources: resources, verb: Installation.reconcileVerb, environment: [:],
                searchPath: [])
            #expect(!command.arguments.contains("--config"))
        }
    }

    @Test func itUsesTheSameInterpreterSearchAsTheEngine() throws {
        try withExecutable(["python3"]) { bin in
            let command = try EngineCommand.resolveInstallation(
                resources: nil, verb: Installation.reconcileVerb, environment: [:],
                searchPath: [bin.path])
            #expect(command.executable == bin.appendingPathComponent("python3").path)
            #expect(command.source == .developerPath)
        }
    }

    @Test func aRunThatCannotStartIsReportedRatherThanThrown() async {
        let report = await InstallationRunner().run(
            EngineCommand(
                executable: "/nowhere/python3", arguments: ["-m", "x", "reconcile"],
                source: .developerPath))

        #expect(report.ok == false)
        #expect(report.failure != nil)
    }

    @Test func aSuccessfulRunCarriesItsOwnWordsAndNoFailure() async {
        // Timed, not only checked: this collected nothing on CI while passing
        // locally, because this process was holding the pipe's write end open
        // and the read could only ever end on the grace timeout. An assertion on
        // the words alone would have gone green again for the wrong reason.
        let started = Date()
        let report = await InstallationRunner().run(
            EngineCommand(
                executable: "/bin/echo", arguments: ["claude-hooks: current"],
                source: .developerPath))
        let waited = Date().timeIntervalSince(started)

        #expect(report.ok)
        #expect(report.lines == ["claude-hooks: current"])
        #expect(report.failure == nil)
        #expect(waited < Installation.grace, "the output arrived on a timeout, not on EOF")
    }

    @Test func aChildThatIgnoresSigtermIsStillStopped() async {
        // The claim is that a reconcile which hangs never becomes an engine that
        // never starts. SIGTERM is a request, and this child refuses it — so the
        // only thing that makes the claim true is the escalation after it.
        let started = Date()
        let report = await InstallationRunner().run(
            EngineCommand(
                executable: "/bin/sh",
                arguments: ["-c", "trap '' TERM; echo holding; while :; do sleep 1; done"],
                source: .developerPath),
            deadline: 1)
        let waited = Date().timeIntervalSince(started)

        #expect(report.ok == false)
        #expect(report.failure?.contains("did not finish within") == true)
        #expect(waited < 1 + (Installation.grace * 3) + 5, "the run was not bounded: \(waited)s")
    }

    @Test func aFailedRunReportsTheFirstThingItSaid() async {
        let report = await InstallationRunner().run(
            EngineCommand(
                executable: "/bin/sh",
                arguments: ["-c", "echo 'claude-hooks: FAILED — a reason'; exit 1"],
                source: .developerPath))

        #expect(report.ok == false)
        #expect(report.failure == "claude-hooks: FAILED — a reason")
    }
}
