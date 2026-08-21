import Foundation

@testable import ShellCore

/// A child that never was: a script of runs, each with how long it stayed up,
/// what it said on stderr, and how it ended.
///
/// Runs the script does not cover stay up until they are asked to stop, which is
/// what a healthy engine does — and one of them can be told to ignore the asking,
/// so the escalation to `SIGKILL` is tested rather than assumed.
final class FakeLauncher: EngineLaunching, @unchecked Sendable {
    struct Run {
        var uptime: TimeInterval
        var code: Int32
        var stderr: String = ""
    }

    private let lock = NSLock()
    private var script: [Run]
    private let clock: TestClock
    /// Whether a child that outlives the script also ignores `requestStop`.
    private let deaf: Bool
    private(set) var launches: [EngineCommand] = []
    private(set) var children: [FakeProcess] = []

    init(script: [Run], clock: TestClock, deaf: Bool = false) {
        self.script = script
        self.clock = clock
        self.deaf = deaf
    }

    func launch(
        _ command: EngineCommand,
        stderr: @escaping @Sendable (Data) -> Void,
        exited: @escaping @Sendable (Int32) -> Void
    ) throws -> EngineProcess {
        let run: Run? = lock.withLock {
            launches.append(command)
            return script.isEmpty ? nil : script.removeFirst()
        }
        let process = FakeProcess(
            pid: Int32(1000 + launchCount()), deaf: deaf, exited: exited)
        lock.withLock { children.append(process) }

        guard let run else { return process }
        if !run.stderr.isEmpty { stderr(Data(run.stderr.utf8)) }
        clock.advance(run.uptime)
        exited(run.code)
        return process
    }

    func launchCount() -> Int { lock.withLock { launches.count } }
    func lastChild() -> FakeProcess? { lock.withLock { children.last } }
}

final class FakeProcess: EngineProcess, @unchecked Sendable {
    let processIdentifier: Int32
    private let deaf: Bool
    private let exited: @Sendable (Int32) -> Void
    private let lock = NSLock()
    private var asked = false
    private var forced = false

    init(pid: Int32, deaf: Bool = false, exited: @escaping @Sendable (Int32) -> Void = { _ in }) {
        processIdentifier = pid
        self.deaf = deaf
        self.exited = exited
    }

    /// A cooperating child stops; a deaf one does not, which is the case the
    /// grace period exists for.
    func requestStop() {
        lock.withLock { asked = true }
        guard !deaf else { return }
        exited(-SIGTERM)
    }

    func forceStop() {
        lock.withLock { forced = true }
        exited(-SIGKILL)
    }

    var stopRequested: Bool { lock.withLock { asked } }
    var stopForced: Bool { lock.withLock { forced } }
}

/// Time the test moves. Sleeping returns at once and is recorded, so the backoff
/// ladder is asserted rather than waited out.
final class TestClock: SupervisorClock, @unchecked Sendable {
    private let lock = NSLock()
    private var current: TimeInterval = 0
    private(set) var slept: [TimeInterval] = []

    var now: TimeInterval { lock.withLock { current } }

    func advance(_ seconds: TimeInterval) {
        lock.withLock { current += seconds }
    }

    func sleep(_ seconds: TimeInterval) async {
        lock.withLock {
            slept.append(seconds)
            current += seconds
        }
        // A sleep of no duration cannot be told apart from no sleep at all, so
        // anything already queued gets to run first. Without this, "the child
        // stopped when asked" and "the grace period expired" race.
        for _ in 0..<16 { await Task.yield() }
    }

    func sleeps() -> [TimeInterval] { lock.withLock { slept } }
}

/// Every health the supervisor passed through, in order.
final class HealthLog: @unchecked Sendable {
    private let lock = NSLock()
    private var entries: [EngineHealth] = []

    func record(_ health: EngineHealth) { lock.withLock { entries.append(health) } }
    func all() -> [EngineHealth] { lock.withLock { entries } }

    /// Wait until the supervisor reaches a state, or give up. Real time, but
    /// only ever a few milliseconds of it.
    func wait(for condition: @escaping (EngineHealth) -> Bool, within seconds: TimeInterval = 5)
        async -> EngineHealth?
    {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if let found = all().last(where: condition) { return found }
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
        return nil
    }
}
