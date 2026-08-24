import Foundation

/// The engine as a **direct child** of this process.
///
/// `Process` is `posix_spawn` underneath, which is what ADR 0005 requires:
/// never `NSWorkspace.open`, never `open(1)`, never a launchd job, and the engine
/// never daemonises itself. `launchctl procinfo` on the child shows this app as
/// the responsible process, which is the probe's own method.
///
/// Bundle containment, not this spawn mechanism, is what earns the microphone
/// grant — but parenthood is what keeps spawn, health and restart in one place.
public struct ProcessLauncher: EngineLaunching {
    public init() {
        // The child's stderr is a pipe this process reads and may stop reading.
        BrokenPipes.ignore()
    }

    public func launch(
        _ command: EngineCommand,
        stderr: @escaping @Sendable (Data) -> Void,
        exited: @escaping @Sendable (Int32) -> Void
    ) throws -> EngineProcess {
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

        let errors = PreAdoptionStderr(deliver: stderr)
        process.standardError = errors.pipe
        errors.read()

        process.terminationHandler = { finished in
            // The residue reaches the Retry panel **before** the exit does; the
            // ordering, and why it is load-bearing, belong to `finish()`.
            errors.finish()
            // A signal is not an exit code. Reported apart so a kill is never
            // mistaken for the engine's own "I could not start".
            let code =
                finished.terminationReason == .uncaughtSignal
                ? -finished.terminationStatus : finished.terminationStatus
            exited(code)
        }

        try process.run()
        return SpawnedEngine(process)
    }
}

final class SpawnedEngine: EngineProcess, @unchecked Sendable {
    private let process: Process

    init(_ process: Process) { self.process = process }

    var processIdentifier: Int32 { process.processIdentifier }

    /// `SIGTERM`, which the engine handles: loops cancelled, adapters closed in
    /// reverse, socket removed. Killing it outright would leave its own debris
    /// for the next start to trip over.
    func requestStop() {
        guard process.isRunning else { return }
        process.terminate()
    }

    /// `SIGKILL`, once the grace period is spent. The debris this leaves is the
    /// next engine's to clear — it probes the socket before refusing, so a dead
    /// one never displaces a live one either way.
    func forceStop() {
        guard process.isRunning else { return }
        kill(process.processIdentifier, SIGKILL)
    }
}
