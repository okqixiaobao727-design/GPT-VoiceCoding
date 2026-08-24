import Foundation
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

        let before = openDescriptors()
        for _ in 0..<attempts {
            #expect(throws: (any Error).self) {
                try ProcessLauncher().launch(missing, stderr: { _ in }, exited: { _ in })
            }
        }
        let leaked = openDescriptors() - before

        // Two per failed launch if the pipe survives; the margin is for whatever
        // else this process opens while these run beside other suites.
        #expect(leaked < attempts / 2, "\(leaked) descriptors outlived \(attempts) failed launches")
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
