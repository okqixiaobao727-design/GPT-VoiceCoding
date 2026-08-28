import Foundation
import ShellTestSupport
import Testing

@testable import ShellCore

/// The real spawn path, against real processes. The supervisor's rules are
/// tested with a fake launcher; this is the other half — that `Process` actually
/// delivers stderr, exit codes and signals the way those rules assume.
@Suite struct ProcessLauncherTests {
    private func run(_ script: String) async -> (code: Int32, stderr: [String], pid: Int32) {
        let collected = Collector()
        let command = EngineCommand(
            executable: "/bin/sh", arguments: ["-c", script], source: .developerPath)
        let process = try! ProcessLauncher().launch(
            command,
            stderr: { collected.add($0) },
            exited: { collected.finish($0) })
        let code = await collected.exitCode()
        return (code, collected.lines(), process.processIdentifier)
    }

    @Test func stderrArrivesAndTheExitCodeIsTheEnginesOwn() async {
        // Exit 2 is "it could not start", with the reason on stderr and nowhere
        // else — this is the pipe that carries it.
        let outcome = await run("echo 'config: [delegate] model is required' 1>&2; exit 2")
        #expect(outcome.code == EngineExitCode.couldNotStart)
        #expect(outcome.stderr == ["config: [delegate] model is required"])
    }

    @Test func aCleanExitIsZeroAndNotASignal() async {
        let outcome = await run("exit 0")
        #expect(outcome.code == EngineExitCode.ok)
    }

    @Test func aSignalIsReportedApartFromAnExitCode() async {
        // A kill must never be mistaken for the engine's own "I could not start".
        let outcome = await run("kill -9 $$")
        #expect(outcome.code == -9)
    }

    @Test func theChildIsThisProcessesOwn() async {
        // Direct `posix_spawn` parenthood, which is what ADR 0005 requires and
        // what `launchctl procinfo` is asked to confirm on a real engine.
        let outcome = await run("exit 0")
        #expect(outcome.pid > 0)
    }

    @Test func everySpawnSaysWhichPathItChoseAndWhereItCameFrom() async {
        // The wiring, not the closure. `LoginShellPath` already proved it calls
        // whatever logger it is handed; what went unproven — and untrue — is
        // that anything ever handed it one, so both of its fallback sentences
        // were discarded in production for the whole of d850e8f's life.
        //
        // A silent fallback is how a feature stops working without anybody
        // noticing, and this ticket is the proof: `-lc` returned a PATH that was
        // usable and wrong, so no fallback line would have fired anyway. Only
        // saying which PATH was *taken* makes that observable.
        let said = Sentences()
        let command = EngineCommand(
            executable: "/bin/sh", arguments: ["-c", "exit 0"], source: .developerPath)
        let collected = Collector()

        _ = try! ProcessLauncher(log: { said.add($0) }).launch(
            command, stderr: { collected.add($0) }, exited: { collected.finish($0) })
        _ = await collected.exitCode()

        // Exactly one, because a line per spawn is a diagnostic and two would be
        // the beginning of a log nobody reads.
        #expect(said.lines.count == 1)
        // Whichever branch this machine takes, the line names the shell it asked
        // and never claims a PATH it did not adopt.
        #expect(said.lines.first?.contains(LoginShellPath.loginShell() ?? "/bin/sh") == true)
    }

    @Test func everySpawnReportsWhatAskingTheLoginShellCameTo() async {
        // The log line is for `log show`; this is for the person looking at the
        // menu bar. Until #118 there was only the first, so an engine running on
        // launchd's four directories — where neither `claude` nor `codex`
        // resolves — looked exactly like a healthy one.
        let seen = Outcomes()
        let collected = Collector()
        let command = EngineCommand(
            executable: "/bin/sh", arguments: ["-c", "exit 0"], source: .developerPath)

        _ = try! ProcessLauncher(
            readPath: { _, _ in .ranOutOfTime },
            report: { seen.add($0) }
        ).launch(command, stderr: { collected.add($0) }, exited: { collected.finish($0) })
        _ = await collected.exitCode()

        #expect(seen.all.count == 1)
        #expect(seen.all.first?.reason != nil)
    }

    @Test func aSpawnThatDidReadAPathReportsThatToo() async {
        // What clears the panel. A report that only fired on failure would leave
        // yesterday's warning up over an engine that is now running on the right
        // PATH — and the user has no way to tell those two apart.
        let seen = Outcomes()
        let collected = Collector()
        let command = EngineCommand(
            executable: "/bin/sh", arguments: ["-c", "exit 0"], source: .developerPath)

        _ = try! ProcessLauncher(
            readPath: { _, _ in .said("/opt/homebrew/bin:/usr/bin") },
            report: { seen.add($0) }
        ).launch(command, stderr: { collected.add($0) }, exited: { collected.finish($0) })
        _ = await collected.exitCode()

        #expect(seen.all.first?.reason == nil)
        if case .adopted(_, let path) = seen.all.first {
            #expect(path == "/opt/homebrew/bin:/usr/bin")
        } else {
            Issue.record("the launcher reported \(String(describing: seen.all.first))")
        }
    }

    @Test func theChildRunsOnThePathTheReaderItWasGivenGave() async {
        // The seam is real wiring and not a test-only parameter: a launcher told
        // where the PATH comes from has to spawn the child on *that* PATH.
        let collected = Collector()
        let command = EngineCommand(
            executable: "/bin/sh", arguments: ["-c", "printf '%s' \"$PATH\" 1>&2"],
            source: .developerPath)

        _ = try! ProcessLauncher(readPath: { _, _ in .said("/opt/only-here") }).launch(
            command, stderr: { collected.add($0) }, exited: { collected.finish($0) })
        _ = await collected.exitCode()

        #expect(collected.lines() == ["/opt/only-here"])
    }

    @Test func theCredentialFileOverridesTheInheritedValueInTheRealChild() async throws {
        let fixture = try TelegramCredentialFixture(tokenVariable: "GVC_LAUNCH_TEST_TOKEN")
        try fixture.writeEnvironment("GVC_LAUNCH_TEST_TOKEN=file-wins\n")

        let collected = Collector()
        let command = EngineCommand(
            executable: "/bin/sh",
            arguments: ["-c", "printf '%s' \"$GVC_LAUNCH_TEST_TOKEN\" 1>&2"],
            source: .developerPath)
        _ = try ProcessLauncher(
            readPath: LoginShellPath.unasked,
            environment: ["GVC_LAUNCH_TEST_TOKEN": "inherited-loses"],
            credentials: fixture.credentials
        ).launch(command, stderr: { collected.add($0) }, exited: { collected.finish($0) })
        _ = await collected.exitCode()

        #expect(collected.lines() == ["file-wins"])
    }

    @Test func aChannelFreeConfigurationIgnoresAnUnusableCredentialFile() async throws {
        for (contents, mode): (String, mode_t) in [
            ("BROKEN", 0o600), ("OLD_TOKEN=leftover\n", 0o644),
        ] {
            let directory = URL(
                fileURLWithPath: "/tmp/gvc-launch-null-channel-\(UUID().uuidString.prefix(8))")
            try FileManager.default.createDirectory(
                at: directory, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: directory) }
            let config = directory.appendingPathComponent("config.toml")
            try Data(
                """
                [adapters]
                companion_channel = "gpt_voicecoding.adapters.companion_channel:null_channel"
                """.utf8
            ).write(to: config)
            let file = directory.appendingPathComponent("environment")
            try Data(contents.utf8).write(to: file)
            #expect(chmod(file.path, mode) == 0)

            let collected = Collector()
            let command = EngineCommand(
                executable: "/bin/sh", arguments: ["-c", "printf started 1>&2"],
                source: .developerPath)
            let credentials = TelegramCredentials(
                configPath: config.path, environmentPath: file.path)

            _ = try ProcessLauncher(
                readPath: LoginShellPath.unasked, credentials: credentials
            ).launch(command, stderr: { collected.add($0) }, exited: { collected.finish($0) })
            _ = await collected.exitCode()

            #expect(collected.lines() == ["started"])
        }
    }

    @Test func aLaunchThatNeverSpawnsKeepsNoPipeAfterwards() {
        // A launch can fail before there is anything to wait for — the engine's
        // binary is missing or not executable, which is the case the supervisor
        // answers with "nothing to spawn". Nothing will ever end that launch's
        // stderr pipe: this process still holds its write end, and a process
        // that never ran never terminates, so the handler that would have been
        // torn down on either event is torn down by neither.
        //
        // Counted in descriptors rather than in objects because that is what
        // runs out. Retry is one press, and a broken install is exactly the
        // state a user presses it from.
        let attempts = 50
        let missing = EngineCommand(
            executable: "/nonexistent/engine", arguments: [], source: .developerPath)

        // Not the real PATH reader: fifty real login shells is ~22 s of somebody's
        // profile (#36), and with a ten-second budget a stuck one would be eight
        // minutes inside a fifteen-minute CI job. Nothing here is about the PATH.
        let launcher = ProcessLauncher(readPath: LoginShellPath.unasked)
        let before = openDescriptors()
        for _ in 0..<attempts {
            #expect(throws: (any Error).self) {
                try launcher.launch(missing, stderr: { _ in }, exited: { _ in })
            }
        }
        let leaked = openDescriptors() - before

        // Two per failed launch if the pipe survives, so the defect this guards
        // is `2 * attempts` — 100 — and the bound is half of that rather than a
        // quarter of it.
        //
        // Re-derived, not loosened. `/dev/fd` is process-wide and swift-testing
        // runs these suites in parallel, so the count includes whatever else is
        // spawning at the same moment. That used to be invisible: with the real
        // login-shell reader this loop took ~22 s and every other suite had long
        // finished by the second sample. Reading no login shell (#36) took it to
        // ~40 ms, which lands both samples inside the churn — measured over 20
        // runs on the reference machine the residue was **−10 to +25**, negative
        // included, which is proof enough that it is not this test's own. The old
        // `attempts / 2` sat exactly on that ceiling and failed 1 run in 15.
        //
        // 50 is twice the worst residue measured and half the defect. What it
        // does *not* claim: that it would catch a leak of one descriptor per
        // launch. That is 50 exactly, and against a −10 sample it reads as 40 and
        // passes — a process-wide counter cannot be made to carry that claim, and
        // asserting it here would be the test saying more than it knows.
        #expect(leaked < attempts, "\(leaked) descriptors outlived \(attempts) failed launches")
    }

    private func openDescriptors() -> Int {
        (try? FileManager.default.contentsOfDirectory(atPath: "/dev/fd").count) ?? 0
    }

    @Test func askingItToStopStopsIt() async {
        let collected = Collector()
        let command = EngineCommand(
            executable: "/bin/sh", arguments: ["-c", "sleep 30"], source: .developerPath)
        let process = try! ProcessLauncher().launch(
            command, stderr: { collected.add($0) }, exited: { collected.finish($0) })

        process.requestStop()

        // SIGTERM: the engine stops in order rather than leaving its socket behind.
        let code = await collected.exitCode()
        #expect(code == -SIGTERM)
    }
}

/// Gathers what the launcher said asking the login shell came to.
private final class Outcomes: @unchecked Sendable {
    private let lock = NSLock()
    private var seen: [LoginShellPath.Outcome] = []

    func add(_ outcome: LoginShellPath.Outcome) { lock.withLock { seen.append(outcome) } }
    var all: [LoginShellPath.Outcome] { lock.withLock { seen } }
}

/// Gathers the launcher's own diagnostic lines, which is a different stream
/// from the child's stderr and must not be confused with it.
private final class Sentences: @unchecked Sendable {
    private let lock = NSLock()
    private var said: [String] = []

    func add(_ line: String) { lock.withLock { said.append(line) } }
    var lines: [String] { lock.withLock { said } }
}

/// Gathers what a real child said and how it ended.
private final class Collector: @unchecked Sendable {
    private let lock = NSLock()
    private var ring = StderrRing()
    private var code: Int32?
    private var waiter: CheckedContinuation<Int32, Never>?

    func add(_ chunk: Data) { lock.withLock { ring.ingest(chunk) } }

    func finish(_ status: Int32) {
        let waiting: CheckedContinuation<Int32, Never>? = lock.withLock {
            code = status
            let waiter = self.waiter
            self.waiter = nil
            return waiter
        }
        waiting?.resume(returning: status)
    }

    func lines() -> [String] { lock.withLock { ring.lines } }

    func exitCode() async -> Int32 {
        await withCheckedContinuation { continuation in
            let finished: Int32? = lock.withLock {
                if let code { return code }
                waiter = continuation
                return nil
            }
            if let finished { continuation.resume(returning: finished) }
        }
    }
}
