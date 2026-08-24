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
/// **It fails open, always.** No shell, a shell that hangs, a non-zero exit, an
/// answer that is not a path — every one of them leaves the inherited
/// environment exactly as it was. This may make a spawn better; it may never
/// make one worse.
///
/// Nothing else is imported from the profile. A login shell can set anything,
/// and a shell that exported a variable into a supervised daemon's environment
/// would be a second, invisible configuration file.
public enum LoginShellPath {
    /// How long the login shell gets. A profile that takes longer than this has
    /// a problem of its own, and the engine is waiting on it.
    public static let timeout: TimeInterval = 2.0

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

    /// What runs the shell. A parameter so the failure modes can be tested
    /// without needing a machine that has each of them.
    public typealias Reader = @Sendable (_ shell: String, _ timeout: TimeInterval) -> String?

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
    public static func applied(
        to environment: [String: String],
        read: Reader = readFromLoginShell,
        log: (String) -> Void = { _ in }
    ) -> [String: String] {
        guard let shell = loginShell(environment: environment) else {
            log("no login shell to ask for a PATH; keeping the one we were given")
            return environment
        }
        guard let answer = read(shell, timeout), let resolved = usable(answer) else {
            log("\(shell) did not give a usable PATH; keeping the one we were given")
            return environment
        }
        // Said on the way past, not only on the way down. The `-lc` defect this
        // replaced produced a PATH that was *usable and wrong*, so no fallback
        // line fired and nothing was written anywhere — the only observable that
        // would have caught it is the one that says which PATH was taken.
        log("PATH from login shell \(shell): \(resolved)")
        var built = environment
        built["PATH"] = resolved
        return built
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
        guard FileManager.default.isExecutableFile(atPath: shell) else { return nil }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        process.arguments = ["-lic", script]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice

        do { try process.run() } catch { return nil }

        // Closed on every path out, including the ones that give up early.
        // `Pipe` hands its read end to a `FileHandle` that outlives this scope —
        // the reader below runs on its own queue and, on the timeout path, is
        // still holding it when we return. One descriptor per spawn, and this
        // is asked on *every* spawn by design, so a crash loop is the case that
        // runs a machine out of them (`aLaunchThatNeverSpawnsKeepsNoPipeAfterwards`).
        let reading = output.fileHandleForReading
        defer { try? reading.close() }

        let collected = ReadToEnd(handle: reading)
        guard collected.wait(timeout) else {
            // A profile that hangs must not hang the engine's supervisor with
            // it. `terminate` first, because a shell given the chance usually
            // takes it; `SIGKILL` is what makes the bound real.
            process.terminate()
            if !process.waitUntil(deadline: .now() + terminationGrace) {
                kill(process.processIdentifier, SIGKILL)
            }
            return nil
        }
        process.waitUntilExit()
        guard process.terminationStatus == 0, process.terminationReason == .exit else { return nil }
        guard let said = String(data: collected.data, encoding: .utf8) else { return nil }
        return delimited(in: said)
    }
}

/// Reading a pipe to EOF with a deadline, which `FileHandle` has no verb for.
private final class ReadToEnd: @unchecked Sendable {
    private let finished = DispatchSemaphore(value: 0)
    private var collected = Data()
    private let lock = NSLock()

    init(handle: FileHandle) {
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let read = (try? handle.readToEnd()) ?? Data()
            lock.lock()
            collected = read
            lock.unlock()
            finished.signal()
        }
    }

    func wait(_ seconds: TimeInterval) -> Bool {
        finished.wait(timeout: .now() + seconds) == .success
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
