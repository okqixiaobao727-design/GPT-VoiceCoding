import Foundation

/// First launch is the install — ADR 0012.
///
/// A `.app` dragged into `/Applications` has no install step: macOS copies a
/// directory and runs nothing. So the shell runs one reconcile before it spawns
/// the engine, and the reconcile writes nothing when the machine already agrees
/// with what this build would put there.
///
/// **The shell is the trigger and nothing more.** The merge, the fingerprint and
/// the reversibility all stay in Python, in one module. A second copy of them
/// here would be the shape of #47, where the control-plane socket path is built
/// independently on both sides with no test holding the two together — so this
/// file knows one verb and how to wait for it, and nothing about settings files.
///
/// **A reconcile that fails does not stop the engine.** What the user loses is
/// reach into their Sessions, which is what the failed item was for; an app that
/// refused to start over it would take the control plane and the Live Call down
/// with it.
public enum Installation {
    /// The only verb the shell uses. `install`, `uninstall` and `status` are the
    /// operator's, through the `bridge-install` console script.
    public static let reconcileVerb = "reconcile"

    /// How long one reconcile may take before the shell stops waiting for it. It
    /// reads and writes two small files; a run that is still going after this is
    /// blocked on something, and the engine should not wait behind it.
    public static let deadline: TimeInterval = 30

    /// How long each step of stopping it is given before the next, harder one.
    /// Short: by the time this is being counted, the run has already failed.
    public static let grace: TimeInterval = 2
}

/// What one reconcile said. The lines are the run's own words, never rephrased.
public struct InstallationReport: Equatable, Sendable {
    public var ok: Bool
    public var lines: [String]

    public init(ok: Bool, lines: [String]) {
        self.ok = ok
        self.lines = lines
    }

    /// The first sentence worth showing a person, or `nil` when nothing is wrong.
    public var failure: String? {
        ok ? nil : (lines.first ?? "the installation could not be reconciled")
    }
}

/// What one run collected, across the queue that collected it.
private final class OutputBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value = Data()

    func set(_ data: Data) {
        lock.lock()
        value = data
        lock.unlock()
    }

    var lines: [String] {
        lock.lock()
        defer { lock.unlock() }
        return String(decoding: value, as: UTF8.self)
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map(String.init)
    }
}

/// Runs one installation verb, and comes back whatever the child does.
///
/// **Every wait here is bounded, and that is the whole design.** This runs
/// *before* the engine is spawned, so a wait without a ceiling is not a slow
/// installation — it is a product that never starts. `waitUntilExit` and
/// `readDataToEndOfFile` are both unbounded, and `SIGTERM` is a request a child
/// may ignore, so neither is used on its own: the run is waited for with a
/// deadline, then asked to stop, then made to stop, and the output is collected
/// with a ceiling of its own because a grandchild holding the pipe would keep it
/// open after the child is gone.
public struct InstallationRunner: Sendable {
    public init() {}

    /// Waits on threads of its own, and never on a pooled one.
    ///
    /// Everything below is blocking — semaphores and `readDataToEndOfFile` — and
    /// whose thread it blocks is not a detail. Swift concurrency's cooperative
    /// pool holds about one thread per core, so a `Task` that blocks one for the
    /// length of a subprocess is a core the whole process cannot use; CI found
    /// this by slowing every unrelated test in the suite by four times. So the
    /// waiting happens on threads created for it and thrown away after, and the
    /// public entry point is `async` and gives its caller's thread back.
    ///
    /// `deadline` is a parameter for one reason: a test that proved the ceiling
    /// by waiting out the real one would take longer than the whole suite.
    public func run(
        _ command: EngineCommand, deadline: TimeInterval = Installation.deadline
    ) async -> InstallationReport {
        await withCheckedContinuation { continuation in
            let thread = Thread {
                continuation.resume(returning: Self.runBlocking(command, deadline: deadline))
            }
            thread.name = "gpt-voicecoding.installation"
            thread.start()
        }
    }

    private static func runBlocking(
        _ command: EngineCommand, deadline: TimeInterval
    ) -> InstallationReport {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: command.executable)
        process.arguments = command.arguments
        if command.source == .bundled {
            // Nothing may write into the bundle at runtime, and a `.pyc` beside a
            // signed file is a modification of a signed bundle.
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process.environment = environment
        }

        let output = Pipe()
        process.standardOutput = output
        process.standardError = output

        let exited = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in exited.signal() }

        do {
            try process.run()
        } catch {
            return InstallationReport(ok: false, lines: ["\(command.executable): \(error)"])
        }

        // This process keeps its own copy of the pipe's write end, and the read
        // below ends when *every* write end is closed — so leaving ours open
        // means the child can exit and the read still never returns. Foundation
        // closes it on some platform versions and not others, which is worse
        // than never closing it: it passes on the machine you wrote it on.
        try? output.fileHandleForWriting.close()

        // Drained from the start, on a thread of its own: a pipe nobody reads
        // fills, and a child blocked on a full pipe is a child that never exits.
        // A thread rather than a queue for the reason in the type's own note —
        // this read has no ceiling of its own, so it must not sit on a shared
        // worker while it waits.
        let collected = OutputBox()
        let drained = DispatchSemaphore(value: 0)
        let reading = output.fileHandleForReading
        let reader = Thread {
            collected.set(reading.readDataToEndOfFile())
            drained.signal()
        }
        reader.name = "gpt-voicecoding.installation.read"
        reader.start()

        if exited.wait(timeout: .now() + deadline) == .timedOut {
            process.terminate()
            if exited.wait(timeout: .now() + Installation.grace) == .timedOut {
                kill(process.processIdentifier, SIGKILL)
                _ = exited.wait(timeout: .now() + Installation.grace)
            }
            _ = drained.wait(timeout: .now() + Installation.grace)
            return InstallationReport(
                ok: false,
                lines: [
                    "the installation reconcile did not finish within "
                        + "\(Int(deadline)) seconds and was stopped"
                ] + collected.lines)
        }

        _ = drained.wait(timeout: .now() + Installation.grace)
        return InstallationReport(ok: process.terminationStatus == 0, lines: collected.lines)
    }
}
