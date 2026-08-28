import Foundation
import os

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
    /// Where this launcher's own sentences go.
    ///
    /// **Not** the engine's log and not ``StderrRing``. ADR 0004 gives the
    /// engine's log to the engine, and the ring carries the engine's own words —
    /// the shell writing its sentences into either would be the surface
    /// inventing speech on the engine's behalf. Choosing a child's environment
    /// is the shell's own act, so it is reported through the platform's
    /// diagnostic channel, where `log show` and Console.app can find it without
    /// the shell keeping a ledger of its own.
    private let log: @Sendable (String) -> Void

    /// The unified log, under the bundle's own identifier.
    ///
    /// The subsystem is read from the bundle rather than spelled out: ADR 0005
    /// makes `Info.plist` the one place that identifier lives, and a copy here
    /// would be a second one to keep in step. Outside a bundle — headless, or a
    /// test — there is no identifier to read, and saying so is more honest than
    /// borrowing the app's.
    public static let unifiedLog: @Sendable (String) -> Void = { line in
        let logger = Logger(
            subsystem: Bundle.main.bundleIdentifier ?? "GPTVoiceCodingShell.unbundled",
            category: "launch")
        // `public` because this is one machine's own `PATH` in its own local
        // log. Default redaction would replace it with `<private>`, which is the
        // same silence this line exists to end.
        logger.notice("\(line, privacy: .public)")
    }

    /// Where the `PATH` comes from. A seam, defaulted to the real login shell,
    /// for one reason: the real one starts `<shell> -lic` and reads somebody's
    /// whole profile, ~0.45 s a spawn on the reference machine, and the Swift
    /// suites that spawn — supervision's five-restart ladder, the descriptor
    /// test's fifty failed launches — pay it without any of them being about the
    /// `PATH`. That is `#36`, and raising the budget to ten seconds raises the
    /// ceiling those suites can hit from 2 s a spawn to 10 s. The suites that
    /// *are* about the `PATH` still take the default.
    private let readPath: LoginShellPath.Reader

    /// The shell-owned environment file, when this launcher is the app's engine
    /// launcher. Nil preserves the general launcher used by headless and focused
    /// process tests; the menu-bar assembly always supplies the shipping credential.
    private let credentials: TelegramCredentials?

    /// Who is told what asking the login shell came to. The launcher chooses the
    /// child's environment; it does not own a surface, so it hands the outcome to
    /// whoever does and keeps no state.
    private let report: @Sendable (LoginShellPath.Outcome) -> Void

    public init(
        log: @escaping @Sendable (String) -> Void = ProcessLauncher.unifiedLog,
        readPath: @escaping LoginShellPath.Reader = LoginShellPath.readFromLoginShell,
        report: @escaping @Sendable (LoginShellPath.Outcome) -> Void = { _ in },
        credentials: TelegramCredentials? = nil
    ) {
        self.log = log
        self.readPath = readPath
        self.report = report
        self.credentials = credentials
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

        // The user's own PATH, read from their login shell — because launchd
        // gives a Finder-launched app `/usr/bin:/bin:/usr/sbin:/sbin`, the engine
        // inherits that, and so does every Session the engine launches. Read
        // every spawn rather than cached: a cached copy of somebody's profile is
        // the staleness this exists to avoid. It fails open, so a spawn is never
        // worse for having asked — and it says so, so a spawn that fell back is
        // never silent either.
        // Timed, because the supervisor cannot see inside this call and would
        // otherwise count the wait as uptime the engine never had.
        let askedAt = Date()
        let credentialEnvironment: [String: String]
        if let credentials {
            let reading = credentials.load()
            guard reading.state.allowsEngineStart else {
                throw TelegramCredentialPreflightFailure(
                    reading.state.failureDetail ?? "Telegram credentials are unavailable")
            }
            credentialEnvironment = reading.environment
        } else {
            credentialEnvironment = [:]
        }

        let path = LoginShellPath.apply(
            to: ProcessInfo.processInfo.environment, read: readPath, log: log)
        let launchOverhead = Date().timeIntervalSince(askedAt)
        // Every spawn, including the ones that worked: the surface clears its
        // own warning by being told the next spawn was fine, and a report that
        // only fired on failure would leave a stale one up for ever.
        report(path.outcome)
        var environment = path.environment
        environment.merge(credentialEnvironment) { _, fromFile in fromFile }

        if command.source == .bundled {
            // Nothing may write into the bundle at runtime, and a `.pyc` beside a
            // signed file is a modification of a signed bundle.
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
        }
        process.environment = environment

        let errors = InheritedStderr(deliver: stderr)
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

        do {
            try process.run()
        } catch {
            // Nothing spawned, so nothing will ever end this pipe: this process
            // still holds the write end, and `terminationHandler` never runs for
            // a process that never ran. Without this the watch — which holds the
            // pipe, which holds the watch — outlives the failed launch, and a
            // Retry against a broken install leaves another one behind each
            // press.
            errors.abandon()
            throw error
        }
        return SpawnedEngine(process, launchOverhead: launchOverhead)
    }
}

final class SpawnedEngine: EngineProcess, @unchecked Sendable {
    private let process: Process

    /// What pre-spawn environment assembly cost, so the supervisor can discount it.
    let launchOverhead: TimeInterval

    init(_ process: Process, launchOverhead: TimeInterval = 0) {
        self.process = process
        self.launchOverhead = launchOverhead
    }

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
