import Darwin
import Foundation

/// The user's real `PATH`, read from the one place that has it.
///
/// A `.app` opened from Finder does not inherit a terminal's environment. macOS
/// gives it launchd's, which is `/usr/bin:/bin:/usr/sbin:/sbin` — no Homebrew,
/// no npm prefix, nothing the user installed. The engine spawned from inside the
/// bundle inherits that, and so does **every Session the engine launches**, since
/// the launcher's environment allowlist carries `PATH` through. So a coding agent
/// started from the menu bar would run without the tools it is there to use.
///
/// The user's `PATH` is already written down, by them, in their shell's own
/// startup files. That is the ledger. Asking them to copy it into
/// `config.toml` would create a second copy that goes stale the day they edit
/// their profile — so this reads the original instead, at the one point where
/// launchd truncated it, and everything downstream inherits the truth for free.
///
/// **Which startup file, though, is the whole difficulty.** The first version of
/// this asked with `-lc` and was wrong on the reference machine: zsh sources
/// `~/.zshrc` only when the shell is *interactive*, and `~/.zshrc` is where a zsh
/// user's `PATH` actually is — it is where `nvm`'s own installer writes itself,
/// and where `brew shellenv` is usually put. A login-but-not-interactive shell
/// reads `.zprofile` and stops, so it hands back a `PATH` the user has never
/// seen, and the engine starts without the coding agent it exists to drive. The
/// ledger was real; `-lc` was reading the wrong page of it.
///
/// So the shell is asked interactively, and the answer is **delimited by
/// sentinels** the shell itself prints around the value. Interactive is what
/// makes a prompt framework — powerlevel10k's instant prompt is the loud one —
/// write to stdout on the way past, and without a delimiter that chatter is
/// indistinguishable from the answer. With one, everything outside the sentinels
/// is noise by construction and the value between them is the `PATH` verbatim.
/// This is the shape VS Code's shell integration has used for years to read an
/// environment out of somebody's real profile; it is settled prior art rather
/// than a local invention.
///
/// **It fails open, always — and never silently.** No shell, a shell that hangs,
/// a non-zero exit, an answer that is not a path — every one of them leaves the
/// inherited environment exactly as it was. This may make a spawn better; it may
/// never make one worse.
///
/// Failing open is right about the *environment* and was wrong about the *user*.
/// The `PATH` left behind is launchd's `/usr/bin:/bin:/usr/sbin:/sbin`, on which
/// neither `claude` nor `codex` resolves, so the engine starts and every Session
/// it launches starts without the agent it exists to drive — and until #118 the
/// only trace was a line in the unified log. So the outcome is now a value
/// (``Outcome``) the caller hands onwards, and the one outcome the user can act
/// on — a login shell that never answered inside its budget — carries a
/// ``Outcome/reason`` for the menu bar to show. Legacy failed loud here and v1.0
/// dropped it: `legacy@1d32845:bridge/terminal.py:893-915` raised
/// `shell_unavailable` and a timeout error rather than carrying on quietly.
/// Ported.
///
/// Nothing else is imported from the profile. A login shell can set anything,
/// and a shell that exported a variable into a supervised daemon's environment
/// would be a second, invisible configuration file.
public enum LoginShellPath {
    /// How long the login shell gets: a measurement with a margin on it, not a
    /// number that looked reasonable.
    ///
    /// The first version of this said 2.0 s on the premise that "a profile that
    /// takes longer than this has a problem of its own". That premise was false
    /// for the reference machine's everyday profile — nvm, `brew shellenv` and a
    /// handful of plugin `bin` entries — and the cost of being wrong is not a
    /// slower start but a silent one: the engine keeps launchd's `PATH` and no
    /// coding agent is on it.
    ///
    /// Measured on the reference machine — 10 cores, an everyday zsh profile —
    /// by ``readFromLoginShell`` itself, load made with real test suites rather
    /// than busy-loops:
    ///
    /// | load average | this reader's wall time |
    /// | --- | --- |
    /// | ~2.5 (idle) | 0.38–0.44 s, 10/10 |
    /// | ~42–52 | 0.49–1.46 s, 20/20 |
    /// | ~89–108 | 0.65–**5.70 s** — 5/10 over the old 2.0 s budget |
    ///
    /// Load ~107 on ten cores is ten times oversubscribed, and 5.70 s is the
    /// worst of every read ever measured here, so this is ≈ 1.75× it. Not less,
    /// because the cost of being under is the whole product — an engine, and
    /// every Session it launches, on a `PATH` with no coding agent on it.
    ///
    /// **What it costs, stated rather than bounded.** The read runs
    /// synchronously inside ``EngineSupervisor``'s actor, so a quit that lands
    /// while one is in flight waits it out before the child is even asked to
    /// stop: up to this budget, then the supervisor's own 5 s stop grace. Ten
    /// seconds is longer than that grace, so this is not bounded by it — it is
    /// the price of the number, and it is paid only by a profile that is stuck,
    /// only by a user who quits in the second it is being read, and it is now
    /// said out loud in the menu bar when it is paid.
    ///
    /// One bounded read and no retry loop: a profile that did not finish in ten
    /// seconds is not going to finish in the next ten either.
    public static let timeout: TimeInterval = 10.0

    /// How long a shell that ignored `SIGTERM` gets before `SIGKILL`. Short,
    /// because a shell that will take the hint takes it immediately and one that
    /// will not is not going to change its mind.
    static let terminationGrace: TimeInterval = 0.2

    /// How finely that grace period is checked. `Process` offers no bounded
    /// `waitUntilExit`, so this is the resolution of the one written here.
    static let terminationPollInterval: useconds_t = 10_000

    /// What marks the answer off from whatever else an interactive profile
    /// decided to print. Long and unlovely on purpose: it has to be something no
    /// prompt framework would emit by accident, because anything it collides
    /// with would be read as a `PATH`.
    static let sentinel = "<<<GVC-PATH>>>"

    /// `printf` rather than `echo`, which appends a newline that then has to be
    /// trimmed, and `%s` rather than `%q`, because this is a value and not a
    /// command line. The sentinels are literal in single quotes so no shell
    /// expands them, and the value stays in `"$PATH"` so no shell splits it.
    static let script =
        #"printf '\#(sentinel)%s\#(sentinel)' "$PATH""#

    /// The `PATH` from between the sentinels, or nothing at all.
    ///
    /// Nothing at all is the right answer for a shell that printed chatter and
    /// never reached the `printf`: it exits 0 having said something that may
    /// well look path-shaped, and taking that would be making a spawn worse.
    ///
    /// **Exactly two, or nothing.** Reading the first two occurrences would, for
    /// output that contains three, take the gap between somebody else's sentinel
    /// and ours — a fragment of a `PATH`, which is path-shaped enough to be
    /// accepted and is a *worse* environment than the one we already had. A
    /// count that is not two means something other than the `printf` wrote the
    /// marker, and then no part of the output can be trusted to be the answer.
    static func delimited(in output: String) -> String? {
        var bounds: [Range<String.Index>] = []
        var searched = output.startIndex..<output.endIndex
        while let found = output.range(of: sentinel, range: searched) {
            bounds.append(found)
            if bounds.count > 2 { return nil }
            searched = found.upperBound..<output.endIndex
        }
        guard bounds.count == 2 else { return nil }
        return String(output[bounds[0].upperBound..<bounds[1].lowerBound])
    }

    /// What a login shell gave back, told apart by *why* when it gave nothing.
    ///
    /// `String?` collapsed four different endings into one `nil`, and they are
    /// not one thing: a shell that printed chatter has told us something about
    /// itself, and a shell still running when the budget ran out has told us
    /// something about the user's afternoon. Only the second is theirs to act
    /// on, so only the second can be reported without turning the panel into a
    /// place people learn to ignore.
    public enum Answer: Equatable, Sendable {
        /// The shell printed something between the sentinels. Whether that
        /// something is a `PATH` is ``usable(_:)``'s question, not this one's.
        case said(String)
        /// The budget ran out with the shell still going.
        case ranOutOfTime
        /// There was no shell to run, it would not start, it failed, or it
        /// exited 0 without ever reaching the `printf`.
        case saidNothing
    }

    /// What asking came to, and the whole of what the caller passes onwards.
    public enum Outcome: Equatable, Sendable {
        /// The login shell's `PATH`, and it is the one the child will run on.
        case adopted(shell: String, path: String)
        /// Nothing to ask. Not the user's problem and not reported to them.
        case noLoginShellToAsk
        /// Asked, and still going when the budget ran out. **The reported one.**
        case ranOutOfTime(shell: String, budget: TimeInterval)
        /// Asked, and what came back was not a `PATH`. Failing open is right
        /// here — the answer would have made the spawn *worse* — so this is a
        /// log line and not a panel.
        case saidNothingUsable(shell: String)

        /// What the user is told, or nothing at all when there is nothing they
        /// can do about it.
        ///
        /// Names the shell, the budget it was given and what the engine is
        /// running on instead, because "could not read your PATH" without those
        /// three is a sentence that sends somebody to the issue tracker.
        ///
        /// Built by concatenation and not from a `"""` literal: this is one
        /// paragraph of prose wrapped to fit the source, and a multi-line literal
        /// keeps whatever indentation the formatter decides to give the
        /// continuation lines — which reached the panel as runs of spaces in the
        /// middle of the sentence the first time this was shown on a real screen.
        public var reason: String? {
            guard case .ranOutOfTime(let shell, let budget) = self else { return nil }
            return "\(shell) did not print a PATH within \(Self.spelled(budget)), so the engine "
                + "— and every Session it launches — is running on the short PATH macOS gives "
                + "an app opened from Finder. A coding agent installed by Homebrew, npm or a "
                + "version manager is not on that PATH and will not be found."
        }

        /// The one line this leaves in the unified log, for every outcome. The
        /// log takes them all; the panel takes one.
        var said: String {
            switch self {
            case .adopted(let shell, let path): return "PATH from login shell \(shell): \(path)"
            case .noLoginShellToAsk:
                return "no login shell to ask for a PATH; keeping the one we were given"
            case .ranOutOfTime(let shell, let budget):
                return "\(shell) did not answer within \(Self.spelled(budget)); "
                    + "keeping the PATH we were given"
            case .saidNothingUsable(let shell):
                return "\(shell) did not give a usable PATH; keeping the one we were given"
            }
        }

        /// Seconds, without the trailing `.0` a `TimeInterval` interpolates and
        /// without inventing a precision the budget does not have.
        private static func spelled(_ seconds: TimeInterval) -> String {
            seconds == seconds.rounded()
                ? "\(Int(seconds))s" : String(format: "%.1fs", seconds)
        }
    }

    /// The environment to spawn with, and what asking came to. Both, because a
    /// caller that only got the environment back had no way to say why it was
    /// the one it already had — which is the whole of #118.
    public struct Applied: Sendable {
        public let environment: [String: String]
        public let outcome: Outcome
    }

    /// What runs the shell. A parameter so the failure modes can be tested
    /// without needing a machine that has each of them — and so a suite that is
    /// not about the `PATH` can decline to start a login shell per spawn at all
    /// (`#36`).
    public typealias Reader = @Sendable (_ shell: String, _ timeout: TimeInterval) -> Answer

    /// The user's login shell: what they told us, else what the password
    /// database says. Never a hard-coded `/bin/zsh` — the default shell has
    /// changed twice in this OS's life and a user may have changed it again.
    public static func loginShell(
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> String? {
        if let stated = environment["SHELL"], !stated.isEmpty { return stated }
        guard let record = getpwuid(getuid()), let name = record.pointee.pw_shell else {
            return nil
        }
        let recorded = String(cString: name)
        return recorded.isEmpty ? nil : recorded
    }

    /// The environment to spawn the engine with: the one we have, with `PATH`
    /// replaced when — and only when — the login shell gave us a usable one.
    ///
    /// Exactly one line is logged, on every path through, including the one that
    /// worked. Said on the way past, not only on the way down: the `-lc` defect
    /// this replaced produced a PATH that was *usable and wrong*, so no fallback
    /// line fired and nothing was written anywhere — the only observable that
    /// would have caught it is the one that says which PATH was taken.
    public static func apply(
        to environment: [String: String],
        read: Reader = readFromLoginShell,
        log: (String) -> Void = { _ in }
    ) -> Applied {
        func settled(_ outcome: Outcome, _ environment: [String: String]) -> Applied {
            log(outcome.said)
            return Applied(environment: environment, outcome: outcome)
        }

        guard let shell = loginShell(environment: environment) else {
            return settled(.noLoginShellToAsk, environment)
        }
        switch read(shell, timeout) {
        case .ranOutOfTime:
            return settled(.ranOutOfTime(shell: shell, budget: timeout), environment)
        case .saidNothing:
            return settled(.saidNothingUsable(shell: shell), environment)
        case .said(let answer):
            guard let resolved = usable(answer) else {
                return settled(.saidNothingUsable(shell: shell), environment)
            }
            var built = environment
            built["PATH"] = resolved
            return settled(.adopted(shell: shell, path: resolved), built)
        }
    }

    /// Whether an answer is a `PATH` at all, rather than a warning a profile
    /// printed on the way past.
    ///
    /// Deliberately shallow: this rejects the shapes that are certainly not a
    /// path list, and does not try to be right about which directories should
    /// exist. A `PATH` naming somewhere that is not there is the user's, and
    /// theirs to have.
    ///
    /// A newline disqualifies the answer **wherever it sits**, so it is looked
    /// for before anything is trimmed away. Trimming first and asking after
    /// would accept `"\n/opt/bin"` — a value that is not what the shell said it
    /// was, arriving from output nobody can account for. Spaces and tabs are
    /// still trimmed, because a `printf` cannot produce them here and a `Reader`
    /// under test may.
    ///
    /// "Newline" means `CharacterSet.newlines` and not `\n` alone. A bare `\r`
    /// is what a profile written on, or for, Windows leaves behind, and it is a
    /// line break to every terminal that will ever render this value — testing
    /// for the one shape a Unix `printf` happens to emit would be checking the
    /// implementation we control instead of the input we do not.
    static func usable(_ answer: String) -> String? {
        guard answer.rangeOfCharacter(from: .newlines) == nil, !answer.contains("\0") else {
            return nil
        }
        let trimmed = answer.trimmingCharacters(in: CharacterSet(charactersIn: " \t"))
        guard !trimmed.isEmpty,
            trimmed.split(separator: ":").contains(where: { $0.hasPrefix("/") })
        else { return nil }
        return trimmed
    }

    /// Run the login shell and take its answer, or nothing at all.
    ///
    /// `-l` is what makes the profile run; without it this reads the same
    /// truncated `PATH` we already have. `-i` is what makes zsh read `~/.zshrc`,
    /// which is the page of the ledger the `PATH` is usually on — and what makes
    /// the sentinels necessary, since an interactive profile may print. stdin is
    /// `/dev/null` and stderr is discarded, so a profile that prompts cannot
    /// block us and a profile that complains cannot be mistaken for the answer.
    public static let readFromLoginShell: Reader = { shell, timeout in
        guard FileManager.default.isExecutableFile(atPath: shell) else { return .saidNothing }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        process.arguments = ["-lic", script]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice

        do { try process.run() } catch { return .saidNothing }

        // Closed on every path out, including the ones that give up early.
        // `Pipe` hands its read end to a `FileHandle` that outlives this scope —
        // the reader below runs on its own queue and, on the timeout path, is
        // still holding it when we return. One descriptor per spawn, and this
        // is asked on *every* spawn by design, so a crash loop is the case that
        // runs a machine out of them (`aLaunchThatNeverSpawnsKeepsNoPipeAfterwards`).
        let reading = output.fileHandleForReading
        defer { try? reading.close() }

        let collected = PipeReader(descriptor: reading.fileDescriptor)

        // **The deadline is on the shell, not on the pipe.** A shell that has
        // printed its answer and exited has told us everything it is going to;
        // whether the *pipe* is closed is a different question with a different
        // answer, because a profile that starts `ssh-agent`, `gpg-agent` or any
        // `&`-ed job hands the write end to something that outlives the shell by
        // hours. Waiting for EOF there spent the whole budget and then threw away
        // a PATH that had arrived in 0.4 s — and, once this began reporting,
        // told a user with an idle machine that their login shell was slow.
        guard process.waitUntil(deadline: .now() + timeout) else {
            // A profile that hangs must not hang the engine's supervisor with
            // it. `terminate` first, because a shell given the chance usually
            // takes it; `SIGKILL` is what makes the bound real.
            process.terminate()
            if !process.waitUntil(deadline: .now() + terminationGrace) {
                kill(process.processIdentifier, SIGKILL)
            }
            // Told apart from every other empty answer, because this is the one
            // the user is shown: their machine was busy, not their profile
            // broken, and no other ending means that.
            return .ranOutOfTime
        }
        process.waitUntilExit()
        guard process.terminationStatus == 0, process.terminationReason == .exit else {
            return .saidNothing
        }
        // The shell's last write happened before it exited, so the bytes are in
        // the pipe; the reader is at most a scheduling quantum behind them. It is
        // given the same short grace a terminating shell gets, and what it has
        // either way is what gets read. **Not** parsed as it arrives: taking the
        // answer the moment two sentinels have been seen would, for output that
        // ends up with three, take the gap between somebody else's sentinel and
        // ours — the truncated PATH `delimited(in:)` exists to refuse, and which
        // `aSentinelInTheNoiseIsNotAnAnswerEither` guards. Exactly two, over
        // everything the shell said, stays the rule.
        collected.settle(terminationGrace)
        guard let said = String(data: collected.data, encoding: .utf8),
            let value = delimited(in: said)
        else { return .saidNothing }
        return .said(value)
    }
}

/// Draining a pipe as it fills, so what has arrived is readable before whoever
/// holds the write end has finished with it.
///
/// Read concurrently rather than after the wait, because a profile that printed
/// more than the pipe's buffer would otherwise block on a pipe nobody is
/// emptying — and the shell that blocked would then be the shell that timed out.
///
/// `read(2)` rather than `FileHandle`: the caller closes the descriptor on the
/// way out while this loop may still be parked on it, which is a case
/// `availableData` answers by raising, and a raised `NSException` here would
/// take the app down over somebody's `ssh-agent`. A closed descriptor is `-1`
/// and `EBADF`, and this loop ends on it like any other error.
private final class PipeReader: @unchecked Sendable {
    private let finished = DispatchSemaphore(value: 0)
    private var collected = Data()
    private let lock = NSLock()

    init(descriptor: Int32) {
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            var buffer = [UInt8](repeating: 0, count: 4096)
            while true {
                let got = buffer.withUnsafeMutableBytes {
                    read(descriptor, $0.baseAddress, $0.count)
                }
                if got > 0 {
                    lock.lock()
                    collected.append(contentsOf: buffer[0..<got])
                    lock.unlock()
                    continue
                }
                if got < 0 && errno == EINTR { continue }
                break  // 0 is EOF; anything else is a descriptor we cannot read
            }
            finished.signal()
        }
    }

    /// Give the reader up to `seconds` to reach EOF. Only ever called once the
    /// shell has already gone, so this is the reader catching up and not a wait
    /// on the shell — which is the whole distinction this class exists to make.
    func settle(_ seconds: TimeInterval) {
        _ = finished.wait(timeout: .now() + seconds)
    }

    var data: Data {
        lock.lock()
        defer { lock.unlock() }
        return collected
    }
}

extension Process {
    /// `waitUntilExit` with a bound, so a child that ignores `SIGTERM` cannot
    /// hold the caller for ever.
    fileprivate func waitUntil(deadline: DispatchTime) -> Bool {
        while isRunning && DispatchTime.now() < deadline {
            usleep(LoginShellPath.terminationPollInterval)
        }
        return !isRunning
    }
}
