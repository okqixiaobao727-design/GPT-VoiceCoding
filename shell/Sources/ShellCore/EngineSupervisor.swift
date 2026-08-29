import Foundation

/// A child of this process, from the supervisor's side.
public protocol EngineProcess: AnyObject, Sendable {
    var processIdentifier: Int32 { get }
    /// How long the launcher spent before this child existed.
    ///
    /// The supervisor times a run from before it asks for the launch, because
    /// that is the only instant it has. `ProcessLauncher` reads the user's login
    /// shell in there — up to ``LoginShellPath/timeout`` of it — and without this
    /// that read is counted as uptime the engine never had. Against
    /// ``RestartPolicy/steadyStateSeconds`` of 60 that is enough to make a
    /// permanently broken engine look like one that took hold, which resets the
    /// failure count and restarts it for ever: the endless silent retry loop
    /// `RestartPolicy` exists to prevent.
    ///
    /// Reported by the launcher rather than sampled by the supervisor so the
    /// supervisor's own `startedAt` does not move — a child that reports nothing
    /// is timed exactly as it was.
    var launchOverhead: TimeInterval { get }
    /// Ask it to stop. `SIGTERM`: the engine stops in order — loops cancelled,
    /// adapters closed in reverse, socket removed — so the next start is not left
    /// claiming its own debris.
    func requestStop()
    /// Make it stop. `SIGKILL`, for a child that did not take the hint.
    func forceStop()
}

extension EngineProcess {
    /// Nothing, for a child whose launcher did no work worth discounting. Every
    /// existing implementation means this, and says so by not saying anything.
    public var launchOverhead: TimeInterval { 0 }
}

/// How the engine is spawned. A protocol so the supervision rules can be tested
/// without a Python interpreter, the same reason every seam here has a fake.
public protocol EngineLaunching: Sendable {
    func launch(
        _ command: EngineCommand,
        stderr: @escaping @Sendable (Data) -> Void,
        exited: @escaping @Sendable (Int32) -> Void
    ) throws -> EngineProcess
}

/// Time, injected, so the backoff ladder is tested rather than waited out.
public protocol SupervisorClock: Sendable {
    var now: TimeInterval { get }
    func sleep(_ seconds: TimeInterval) async
}

public struct SystemClock: SupervisorClock {
    public init() {}
    public var now: TimeInterval { Date().timeIntervalSinceReferenceDate }
    public func sleep(_ seconds: TimeInterval) async {
        try? await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
    }
}

/// The typed reason supervision could not create a child, without discarding the
/// user-facing detail each source already knows.
public enum CannotSpawnReason: Equatable, Sendable {
    case command(String)
    case credentials(TelegramCredentials.State)
    case launch(String)

    public var detail: String {
        switch self {
        case .command(let detail), .launch(let detail): return detail
        case .credentials(let state):
            return state.failureDetail ?? "Telegram credentials are unavailable"
        }
    }
}

/// What the shell knows about its child, and nothing it inferred about the engine
/// itself. Whether the engine is *usable* is a control-plane question, asked
/// separately; this is process parenthood alone.
public enum EngineHealth: Equatable, Sendable {
    case notStarted
    case running(pid: Int32)
    /// Died, and will be spawned again after this delay.
    case restarting(after: TimeInterval, attempt: Int)
    /// Stopped spawning, on purpose, and waiting for a person.
    case stopped(StopReason)
    /// A child could not be created. The typed reason says which launch boundary refused it.
    case cannotSpawn(CannotSpawnReason)
    /// Stopped because the shell is going away.
    case shutDown
}

/// Spawn, health, restart — the shell's only relationship to the engine beyond
/// the control plane (ADR 0001, ADR 0005).
///
/// It restarts on **every** exit, including a clean one: an exit-0 crash class
/// took the old Bridge down and `KeepAlive: true` is the lesson. It also stops,
/// visibly, when restarting is plainly not working — an endless silent retry loop
/// looks exactly like a healthy system to the only person who could fix it.
public actor EngineSupervisor {
    private let launcher: EngineLaunching
    private let clock: SupervisorClock
    private let policy: RestartPolicy
    private let socketPath: String
    private let resolveCommand: @Sendable () throws -> EngineCommand
    private let socketAnswers: @Sendable (String) -> Bool
    private var observer: @Sendable (EngineHealth) -> Void

    public private(set) var health: EngineHealth = .notStarted {
        didSet { if health != oldValue { observer(health) } }
    }
    /// What the engine last said on stderr, held verbatim.
    public private(set) var stderr = StderrRing()

    private var consecutiveFastFailures = 0
    private var child: EngineProcess?
    private var exitWaiter: CheckedContinuation<Int32, Never>?
    private var unclaimedExit: Int32?
    /// Whether the child running now has reported its exit. Reset per spawn.
    private var childHasExited = false
    private var supervision: Task<Void, Never>?
    /// Set before the task exists, cleared when it returns. `supervision` alone
    /// cannot answer this: a run that finishes before `start` has assigned the
    /// handle would leave a completed task sitting where "nothing is running"
    /// belongs, and `retry` would refuse to do anything.
    private var supervising = false
    private var shuttingDown = false

    public init(
        launcher: EngineLaunching,
        socketPath: String,
        resolveCommand: @escaping @Sendable () throws -> EngineCommand,
        clock: SupervisorClock = SystemClock(),
        policy: RestartPolicy = RestartPolicy(),
        socketAnswers: @escaping @Sendable (String) -> Bool = {
            SocketOwnership.isConnectable($0)
        },
        observer: @escaping @Sendable (EngineHealth) -> Void = { _ in }
    ) {
        self.launcher = launcher
        self.socketPath = socketPath
        self.resolveCommand = resolveCommand
        self.clock = clock
        self.policy = policy
        self.socketAnswers = socketAnswers
        self.observer = observer
    }

    /// Watch every health change from here on — and be told the current one at
    /// once, so an observer that arrives after the first spawn is not left
    /// showing "not started" over a running engine.
    public func observe(_ observer: @escaping @Sendable (EngineHealth) -> Void) {
        self.observer = observer
        observer(health)
    }

    /// Begin supervising. Idempotent while a run is in flight.
    public func start() {
        guard !supervising else { return }
        supervising = true
        shuttingDown = false
        supervision = Task { await self.supervise() }
    }

    /// The manual way out of a stopped state. It forgives the failure count and
    /// starts a fresh ring, because the person pressing it is asserting that
    /// whatever was wrong has been dealt with.
    public func retry() {
        guard !supervising else { return }
        consecutiveFastFailures = 0
        stderr.clear()
        start()
    }

    /// How long a child gets to stop in order before it is stopped outright.
    ///
    /// Bounded on purpose: a shell that cannot quit because its child ignores
    /// `SIGTERM` is the same dishonest wait this whole design exists to remove.
    /// The socket a killed engine leaves behind is not this shell's problem —
    /// the next engine probes before refusing, and clears debris it can prove is
    /// debris.
    public static let stopGraceSeconds: TimeInterval = 5

    /// Stop the child and do not spawn another. The shell is going away.
    ///
    /// The loop is parked on the child's exit, so asking the child to stop is
    /// what releases it — and the deadline below is what guarantees that
    /// happens whether or not the child cooperates.
    public func shutDown() async {
        shuttingDown = true
        guard let stopping = child else {
            await supervision?.value
            supervision = nil
            health = .shutDown
            return
        }

        stopping.requestStop()
        let deadline = Task { [clock] in
            await clock.sleep(Self.stopGraceSeconds)
            guard !Task.isCancelled else { return }
            await self.forceIfStillAlive(stopping)
        }
        await supervision?.value
        deadline.cancel()
        supervision = nil
        health = .shutDown
    }

    public func lines() -> [String] { stderr.lines }

    // MARK: - The loop

    /// The loop, and the one rule about how it ends: `supervising` is cleared
    /// **before** the state that ends it is published.
    ///
    /// A person seeing "the engine keeps failing to start" may press Retry in the
    /// same instant. If the flag were still set at that moment, Retry would
    /// silently do nothing; if Retry waited for the loop instead, pressing it
    /// during a healthy run would wait forever. Publishing last settles both.
    private func supervise() async {
        var ending: EngineHealth?
        while !shuttingDown, ending == nil {
            let command: EngineCommand
            do {
                command = try resolveCommand()
            } catch let failure as EngineCommandFailure {
                ending = .cannotSpawn(.command(failure.detail))
                break
            } catch {
                ending = .cannotSpawn(.command("\(error)"))
                break
            }

            // The ring captures from spawn, not from failure: the lines that
            // explain a death are the ones before it.
            stderr.clear()
            childHasExited = false
            let startedAt = clock.now
            let process: EngineProcess
            do {
                process = try launcher.launch(
                    command,
                    stderr: { [weak self] chunk in
                        Task { await self?.received(chunk) }
                    },
                    exited: { [weak self] code in
                        Task { await self?.childExited(code) }
                    })
            } catch let failure as TelegramCredentialPreflightFailure {
                ending = .cannotSpawn(.credentials(failure.state))
                break
            } catch {
                ending = .cannotSpawn(.launch("\(error)"))
                break
            }
            child = process
            // Read now, while the child is still here to be asked.
            let overhead = process.launchOverhead
            health = .running(pid: process.processIdentifier)

            let code = await nextExit()
            child = nil
            if shuttingDown { break }

            // The launcher's own work discounted, so a slow login shell is not
            // uptime the engine never had. Floored at zero: a launcher that
            // over-reports must not be able to invent a negative run.
            let exit = EngineExit(
                code: code, ranFor: max(0, clock.now - startedAt - overhead))
            let probe: SocketProbe =
                policy.probesTheSocket(after: exit)
                ? (socketAnswers(socketPath) ? .answered : .silent)
                : .notProbed

            switch policy.verdict(
                after: exit, socket: probe, consecutiveFastFailures: consecutiveFastFailures)
            {
            case .giveUp(let reason):
                ending = .stopped(reason)
            case .restart(let delay, let failures):
                consecutiveFastFailures = failures
                health = .restarting(after: delay, attempt: failures + 1)
                await clock.sleep(delay)
            }
        }

        // Cleared first, published second. See the note above.
        supervising = false
        if let ending, !shuttingDown { health = ending }
        if shuttingDown { health = .shutDown }
    }

    private func nextExit() async -> Int32 {
        if let claimed = unclaimedExit {
            unclaimedExit = nil
            return claimed
        }
        return await withCheckedContinuation { continuation in
            exitWaiter = continuation
        }
    }

    private func received(_ chunk: Data) {
        stderr.ingest(chunk)
    }

    /// The grace period is over. Kill it only if it has not already gone: a
    /// child that stopped when it was asked must never be killed for it, and a
    /// pid that has been reaped may belong to somebody else by now.
    private func forceIfStillAlive(_ process: EngineProcess) {
        guard !childHasExited else { return }
        process.forceStop()
    }

    private func childExited(_ code: Int32) {
        childHasExited = true
        if let waiter = exitWaiter {
            exitWaiter = nil
            waiter.resume(returning: code)
        } else {
            // The exit beat the loop to the await. Hold it rather than lose it.
            unclaimedExit = code
        }
    }
}
