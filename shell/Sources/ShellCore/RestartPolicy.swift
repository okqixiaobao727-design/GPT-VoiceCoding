import Foundation

/// How a run of the engine ended, from the shell's side of process parenthood.
public struct EngineExit: Equatable, Sendable {
    /// The engine's own numbers: 0 is a clean stop, 2 is "it could not start".
    public var code: Int32
    /// How long it had been up. The only measure of whether a start took hold.
    public var ranFor: TimeInterval

    public init(code: Int32, ranFor: TimeInterval) {
        self.code = code
        self.ranFor = ranFor
    }
}

/// What dialling the control socket found after an exit that might mean
/// "somebody else is already listening".
public enum SocketProbe: Equatable, Sendable {
    /// Not asked, because this exit code cannot mean that.
    case notProbed
    /// Something is behind the socket. It is not this shell's child.
    case answered
    /// Nothing answered.
    case silent
}

/// Why the shell stopped spawning and started waiting for a person.
public enum StopReason: Equatable, Sendable {
    /// The engine kept dying before it could take hold.
    case repeatedFailures(attempts: Int)
    /// An engine this shell did not start is already on the socket.
    case anotherEngineIsListening
}

public enum RestartVerdict: Equatable, Sendable {
    case restart(after: TimeInterval, consecutiveFastFailures: Int)
    case giveUp(StopReason)
}

/// The shell's restart rules.
///
/// Restarting on *every* exit, including a clean one, is the `KeepAlive: true`
/// lesson: an exit-0 crash class took the old Bridge down and nothing brought it
/// back. Giving up after a bounded number of failures is the other half of the
/// same honesty — an endless silent retry loop looks exactly like a healthy
/// system to the only person who could fix it.
public struct RestartPolicy: Sendable {
    /// The first delay, doubled per consecutive fast failure, up to ``delayCap``.
    public var firstDelay: TimeInterval = 1
    public var delayCap: TimeInterval = 30
    /// How long a run must last before it counts as having taken hold. This is
    /// the single definition of "fast failure": anything shorter is one.
    public var steadyStateSeconds: TimeInterval = 60
    /// Consecutive fast failures the shell will absorb before it stops and says so.
    public var failureCeiling: Int = 5

    public init() {}

    /// Died before it had been up long enough to count as started.
    public func isFastFailure(ranFor: TimeInterval) -> Bool {
        ranFor < steadyStateSeconds
    }

    /// Exit 2 is the only code that can mean "a second engine refused because one
    /// is already serving", so it is the only one worth a probe.
    public func probesTheSocket(after exit: EngineExit) -> Bool {
        exit.code == EngineExitCode.couldNotStart
    }

    /// The delay before the *n*th consecutive fast failure is retried.
    public func delay(forFastFailure n: Int) -> TimeInterval {
        guard n > 1 else { return firstDelay }
        return min(firstDelay * pow(2, TimeInterval(n - 1)), delayCap)
    }

    public func verdict(
        after exit: EngineExit, socket: SocketProbe, consecutiveFastFailures: Int
    ) -> RestartVerdict {
        if socket == .answered && probesTheSocket(after: exit) {
            // A failed start is not evidence the running engine is gone, so this
            // neither counts as a crash nor licenses another spawn against a live
            // socket.
            return .giveUp(.anotherEngineIsListening)
        }
        guard isFastFailure(ranFor: exit.ranFor) else {
            return .restart(after: firstDelay, consecutiveFastFailures: 0)
        }
        let failures = consecutiveFastFailures + 1
        guard failures < failureCeiling else {
            return .giveUp(.repeatedFailures(attempts: failures))
        }
        return .restart(after: delay(forFastFailure: failures), consecutiveFastFailures: failures)
    }
}

/// The engine's exit codes, as `engine/runner.py` defines them.
public enum EngineExitCode {
    public static let ok: Int32 = 0
    /// It could not start, and said why on stderr — before it adopts its own log
    /// (ADR 0004), so stderr is the only place that reason exists.
    public static let couldNotStart: Int32 = 2
}
