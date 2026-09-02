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
    ///
    /// Only the grace period. The budget itself is waited on with the process's
    /// own termination handler, because polling *that* at this resolution would
    /// be a thousand wakeups per engine start on a stuck profile.
    static let terminationPollInterval: useconds_t = 10_000

    /// How often the pipe reader looks up from the pipe to see whether it has
    /// been asked to stop. Its own number rather than ``terminationPollInterval``
    /// because it answers a different question — one is how patient we are with a
    /// shell ignoring `SIGTERM`, the other is how long a reader with nothing to
    /// read may sit before it notices the call has ended.
    static let readerStopCheckInterval: TimeInterval = 0.01

    /// How much a reader that has been asked to stop will still take before it
    /// goes.
    ///
    /// One pipe buffer. Everything the shell wrote before it exited is already
    /// in the pipe, and Darwin's pipe holds 64 KiB, so this is "all of it" stated
    /// as a number — and it has to *be* a number, because a profile that
    /// backgrounds something which keeps writing would otherwise offer an endless
    /// supply and the stop would never come.
    static let drainCap = 64 * 1024

    /// The most this will hold from one shell.
    ///
    /// A `PATH` is a few hundred bytes and the sentinels bound it. Anything past
    /// this is a profile writing to stdout in a loop, and reading it to the end
    /// would be an unbounded buffer in the engine's supervisor for as long as the
    /// loop runs.
    static let collectionCap = 1024 * 1024

    /// How long the reader gets to finish draining once the shell has exited.
    ///
    /// Its own number for the same reason. The shell's last write happened
    /// before it exited, so this is a scheduling delay and not a wait on
    /// anybody's profile.
    static let readerSettle: TimeInterval = 0.2

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
        /// The shell finished, and this side never got what it wrote.
        case notCollected
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
        /// Asked, answered, and **we** did not get the answer: no descriptor to
        /// read it with, or nothing scheduling the reader that would have. Told
        /// apart from ``ranOutOfTime`` because the shell did nothing wrong, and
        /// a panel that blames somebody's `.zshrc` for this app's thread pool
        /// sends them to edit a file that was never the problem. Reported all the
        /// same: the engine is on launchd's `PATH` either way.
        case answerNotCollected(shell: String)

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
            let consequence =
                "the engine — and every Session it launches — is running on the short PATH "
                + "macOS gives an app opened from Finder. A coding agent installed by "
                + "Homebrew, npm or a version manager is not on that PATH and will not be found."
            switch self {
            case .ranOutOfTime(let shell, let budget):
                return "\(shell) did not print a PATH within \(Self.spelled(budget)), so "
                    + consequence
            case .answerNotCollected(let shell):
                // Whose fault it was, said plainly, because the fix is not in
                // their profile and pointing them at it wastes their afternoon.
                return "\(shell) answered, but this app did not manage to read the answer, so "
                    + consequence + " That is this app's own doing and not your profile's."
            case .adopted, .noLoginShellToAsk, .saidNothingUsable:
                return nil
            }
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
            case .answerNotCollected(let shell):
                return "\(shell) answered but the answer was never collected; "
                    + "keeping the PATH we were given"
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
        case .notCollected:
            return settled(.answerNotCollected(shell: shell), environment)
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

        let askedAt = Date()
        // Set before `run`, so an exit that beats us to it still signals.
        let exited = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in exited.signal() }

        do { try process.run() } catch { return .saidNothing }

        // This closes the descriptor **this scope** owns, and only that one. The
        // reader below is given a `dup` of its own and closes that itself; the
        // two are separate numbers with separate owners, which is the whole of
        // why the reader can outlive this call safely. One descriptor per spawn
        // on each side, and this is asked on *every* spawn by design, so a crash
        // loop is the case that runs a machine out of them
        // (`aLaunchThatNeverSpawnsKeepsNoPipeAfterwards`).
        //
        // Closing is **not** how the reader is stopped. An earlier version of
        // this file made it so and was wrong; `PipeReader`'s own note has the two
        // orderings and which of them that version measured.
        let reading = output.fileHandleForReading
        defer { try? reading.close() }

        // The reader gets a descriptor of its **own**, and is the only thing that
        // ever closes it.
        //
        // `58226cc` had the caller close the one the reader was using, on the
        // measured ground that closing wakes a Darwin `read(2)` with 0. That
        // measurement is true and was the wrong half of the question: it holds
        // when the reader is *parked* in `read`, and says nothing about a reader
        // that has just taken a chunk and not yet asked for the next one. In that
        // window the number is freed while the reader still holds it — and the
        // very next `Pipe()` in `ProcessLauncher.launch` is the engine's stderr,
        // which takes the number back. The reader would then eat the engine's
        // own account of why it could not start (ADR 0004 leaves that on stderr
        // and nowhere else) and park on it for ever. Close-to-wake is the
        // hazard, not the cure.
        let collected = PipeReader(duplicating: reading.fileDescriptor)

        // **The deadline is on the shell, not on the pipe.** A shell that has
        // printed its answer and exited has told us everything it is going to;
        // whether the *pipe* is closed is a different question with a different
        // answer, because a profile that starts `ssh-agent`, `gpg-agent` or any
        // `&`-ed job hands the write end to something that outlives the shell by
        // hours. Waiting for EOF there spent the whole budget and then threw away
        // a PATH that had arrived in 0.4 s — and, once this began reporting, told
        // a user with an idle machine that their login shell was slow.
        //
        // Waited on the process's own termination handler rather than polled: at
        // ten milliseconds a ten-second budget is a thousand wakeups per engine
        // start, for a fact the kernel will hand us for nothing.
        guard exited.wait(timeout: .now() + timeout) == .success else {
            // A profile that hangs must not hang the engine's supervisor with
            // it. `terminate` first, because a shell given the chance usually
            // takes it; `SIGKILL` is what makes the bound real.
            process.terminate()
            if !process.waitUntil(deadline: .now() + terminationGrace) {
                kill(process.processIdentifier, SIGKILL)
            }
            collected.stopAndWait(readerSettle)
            // Told apart from every other empty answer, because this is the one
            // the user is shown: their machine was busy, not their profile
            // broken, and no other ending means that.
            return .ranOutOfTime
        }
        process.waitUntilExit()
        guard process.terminationStatus == 0, process.terminationReason == .exit else {
            collected.stopAndWait(readerSettle)
            return .saidNothing
        }

        // The shell's last write happened before it exited, so the bytes are in
        // the pipe; the reader is at most a scheduling quantum behind them. It
        // gets that quantum, and then it is asked to stop and waited for — which
        // is what keeps a reader that never got scheduled from being reported as
        // a shell that said nothing.
        //
        // On this path the wait also means the descriptor is closed by the time
        // this returns. On the give-up paths it is asked to stop and given a
        // bounded window, and may still be going when they return. That costs a
        // descriptor and a thread until it next runs; it costs no *correctness*,
        // because the descriptor is the reader's own and nothing else will ever
        // free the number under it. That is the difference the `dup` buys, and
        // the reason the give-up paths can afford not to wait.
        //
        // The window is always waited out rather than skipped once an answer is
        // in hand: the reader may be holding the first two sentinels while a
        // third is still unread in the pipe, and taking the answer then is the
        // truncated PATH again. So a profile that backgrounds something pays
        // `readerSettle` once per engine start, against the whole budget the same
        // profile used to spend.
        return answer(
            draining: collected,
            remaining: max(0, timeout - Date().timeIntervalSince(askedAt)))
    }

    /// What a shell that has already exited said, once its reader has let go.
    ///
    /// Its own function so the one case that cannot be staged with a real shell
    /// can be: a reader that is never scheduled at all. A seam rather than a
    /// contrivance — the caller passes the real ``PipeReader`` and a test passes
    /// something that never finishes.
    static func answer(draining pipe: DrainedPipe, remaining: TimeInterval) -> Answer {
        // No descriptor to read with — `dup` refused, which on a crash loop means
        // the descriptors ran out. Nothing was collected and nothing will be, and
        // calling that "the shell said nothing" is the silent launchd `PATH` this
        // whole outcome type exists to end, through a second door.
        guard !pipe.failed else { return .notCollected }

        // The settle and the join share what is left of the budget rather than
        // being added to it: this call promised to be over inside `timeout`.
        // The join keeps a floor of two stop-checks even so, because a join of
        // zero cannot observe a reader that leaves within one — a floor of
        // twenty milliseconds, and the alternative is a bound that cannot tell
        // "gone" from "never asked".
        let settle = max(0, min(readerSettle, remaining))
        if !pipe.awaitEnd(settle) {
            pipe.stop()
            // Bounded by what is left of the budget: the reader leaves within one
            // stop-check of being asked, unless nothing is scheduling it at all.
            //
            // That case is the reason this is a `guard` and not a shrug. With no
            // reader there are no bytes, and calling no bytes "the shell said
            // nothing" would put the engine on launchd's PATH **silently** — the
            // one hole left in "it never fails silently", and likeliest at the
            // very load the budget was sized for, because the app blocks
            // global-queue threads elsewhere too.
            let join = max(readerStopCheckInterval * 2, remaining - settle)
            guard pipe.awaitEnd(join) else { return .notCollected }
        }

        // **Not** parsed as it arrives: taking the answer the moment two
        // sentinels have been seen would, for output that ends up with three,
        // take the gap between somebody else's sentinel and ours — the truncated
        // PATH `delimited(in:)` exists to refuse, and which
        // `aSentinelInTheNoiseIsNotAnAnswerEither` guards. Exactly two, over
        // everything the shell said, stays the rule.
        guard let said = String(data: pipe.data, encoding: .utf8),
            let value = delimited(in: said)
        else { return .saidNothing }
        return .said(value)
    }
}

/// What the deadline logic needs of a reader, and no more.
///
/// Small on purpose: the only implementation that matters is ``PipeReader``, and
/// the only reason this exists is that the failure worth proving — a reader that
/// is never scheduled — cannot be staged with a real one.
protocol DrainedPipe {
    /// Wait for the reader to finish, which is to say for its descriptor to be
    /// closed. `false` means what it holds is not the whole answer.
    func awaitEnd(_ seconds: TimeInterval) -> Bool
    /// Ask it to leave, once there is nothing left to read.
    func stop()
    var data: Data { get }
    /// Whether this never had a descriptor to read with at all.
    var failed: Bool { get }
}

/// Draining a pipe as it fills, and owning the descriptor it drains.
///
/// Read concurrently rather than after the wait, because a profile that printed
/// more than the pipe's buffer would otherwise block on a pipe nobody is
/// emptying — and the shell that blocked would then be the shell that timed out.
///
/// **It closes its own descriptor and nothing else may.** The read end is
/// `dup`ed at construction so the caller can close the original whenever it
/// likes without touching this one. That is not belt-and-braces; it is the whole
/// correctness argument. Closing a descriptor a thread is *parked* in `read(2)`
/// on does wake it, on Darwin, with 0 — measured. Closing one that a thread has
/// merely finished a chunk on and not yet re-entered `read` with does something
/// else entirely: it frees the **number** while that thread still holds it, and
/// in `ProcessLauncher.launch` the very next `Pipe()` is the engine's stderr,
/// which takes the number back. The reader would then consume the engine's own
/// account of why it could not start — which ADR 0004 leaves on stderr and
/// nowhere else — and park on it for ever, costing a global-queue thread with it.
/// Two orderings, and an earlier version of this file measured only the first
/// and concluded from it. Both are written down here so the next reader does not
/// have to find the second one the hard way.
///
/// So stopping is by flag and never by close. `poll(2)` bounds how long this can
/// sit with nothing to read, the flag is checked only when the poll finds nothing
/// pending — so everything already written is taken before it leaves — and the
/// caller waits for it to finish — which on the path that returns an answer makes
/// "the descriptor is closed" true at the moment the caller returns, and on the
/// give-up paths is a bounded courtesy rather than a guarantee. Either way no
/// other code can free the number, which is the property that matters.
///
/// `read(2)` rather than `FileHandle.availableData` throughout: the latter
/// answers an unreadable descriptor by raising, and a raised `NSException` here
/// would take the app down over somebody's `ssh-agent`.
/// Internal rather than private only so `LoginShellPathTests` can construct one
/// over a pipe it owns: the stop-check at the top of the loop below is what
/// `aReaderWhosePollIsNeverEmptyStillLeavesWhenAsked` proves, and no public path
/// can stage an always-ready pipe without measuring a clock instead (#205).
final class PipeReader: DrainedPipe, @unchecked Sendable {
    private let finished = DispatchSemaphore(value: 0)
    private var collected = Data()
    private var stopping = false
    private var couldNotStart = false
    private let lock = NSLock()

    init(duplicating descriptor: Int32) {
        let owned = dup(descriptor)
        guard owned >= 0 else {
            // `EMFILE`, which is the crash-loop case. Recorded rather than
            // shrugged off: with no descriptor there are no bytes, and no bytes
            // must not be read back as "the shell said nothing".
            lock.withLock { couldNotStart = true }
            finished.signal()
            return
        }
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            // The one close, by the one owner, on every way out of the loop.
            defer {
                close(owned)
                finished.signal()
            }
            let milliseconds = Int32(LoginShellPath.readerStopCheckInterval * 1000)
            var buffer = [UInt8](repeating: 0, count: 4096)
            while true {
                // **Checked first, every time round.** Checking it only when the
                // poll found nothing pending is what an earlier version did, and
                // a profile that backgrounds something which keeps *writing* then
                // never lets this leave: the poll is always ready, the read always
                // succeeds, and the stop is never seen. The PATH was in hand and
                // the call still ran out its budget.
                if askedToStop {
                    drainPending(owned, &buffer)
                    return
                }
                var watched = pollfd(fd: owned, events: Int16(POLLIN), revents: 0)
                let ready = poll(&watched, 1, milliseconds)
                if ready < 0 {
                    if errno == EINTR { continue }
                    return
                }
                if ready == 0 { continue }
                let got = buffer.withUnsafeMutableBytes {
                    read(owned, $0.baseAddress, $0.count)
                }
                if got > 0 {
                    guard keep(buffer, got) else { return }
                    continue
                }
                if got < 0 && errno == EINTR { continue }
                return  // 0 is EOF; anything else is a descriptor we cannot read
            }
        }
    }

    /// Everything already pending, and then no more.
    ///
    /// Bounded by one pipe buffer, which is all a shell that has already exited
    /// can have left behind — and a bound rather than "until `EAGAIN`" because a
    /// background writer never reaches `EAGAIN` and this must end.
    private func drainPending(_ owned: Int32, _ buffer: inout [UInt8]) {
        var taken = 0
        while taken < LoginShellPath.drainCap {
            var watched = pollfd(fd: owned, events: Int16(POLLIN), revents: 0)
            guard poll(&watched, 1, 0) > 0 else { return }
            let got = buffer.withUnsafeMutableBytes { read(owned, $0.baseAddress, $0.count) }
            if got > 0 {
                taken += got
                guard keep(buffer, got) else { return }
                continue
            }
            if got < 0 && errno == EINTR { continue }
            return
        }
    }

    /// Hold what was read, or say that we are full. `false` ends the loop: a
    /// profile writing to stdout for ever must not become an unbounded buffer
    /// inside the engine's supervisor.
    private func keep(_ buffer: [UInt8], _ count: Int) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard collected.count < LoginShellPath.collectionCap else { return false }
        collected.append(contentsOf: buffer[0..<count])
        return true
    }

    /// Ask it to leave. Takes effect within one ``LoginShellPath/readerStopCheckInterval``,
    /// whatever the profile is doing to the pipe.
    func stop() { lock.withLock { stopping = true } }

    private var askedToStop: Bool { lock.withLock { stopping } }

    /// Wait for the reader to finish — which is to say, for its descriptor to be
    /// closed. `false` means it is still going, and the caller must not treat
    /// what it has as the whole answer.
    ///
    /// Safe to call more than once: a timed-out wait consumes no signal, so a
    /// short window followed by a longer one is two questions about one event.
    @discardableResult
    func awaitEnd(_ seconds: TimeInterval) -> Bool {
        finished.wait(timeout: .now() + seconds) == .success
    }

    /// Leave, and wait — for the paths that are on their way out and only need
    /// the descriptor accounted for.
    func stopAndWait(_ seconds: TimeInterval) {
        stop()
        awaitEnd(seconds)
    }

    var data: Data {
        lock.lock()
        defer { lock.unlock() }
        return collected
    }

    var failed: Bool { lock.withLock { couldNotStart } }
}

extension Process {
    /// `waitUntilExit` with a bound, so a child that ignores `SIGTERM` cannot
    /// hold the caller for ever.
    ///
    /// Polled, and only ever over ``LoginShellPath/terminationGrace`` — two
    /// tenths of a second, which is twenty wakeups. The budget itself is waited
    /// on with the process's own termination handler, because polling *that* at
    /// this resolution would be a thousand wakeups per engine start.
    fileprivate func waitUntil(deadline: DispatchTime) -> Bool {
        while isRunning && DispatchTime.now() < deadline {
            usleep(LoginShellPath.terminationPollInterval)
        }
        return !isRunning
    }
}
