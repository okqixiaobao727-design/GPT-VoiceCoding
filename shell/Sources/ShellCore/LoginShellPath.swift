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

    /// `printf` rather than `echo`, which appends a newline that then has to be
    /// trimmed, and `%s` rather than `%q`, because this is a value and not a
    /// command line.
    static let script = #"printf %s "$PATH""#

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
    static func usable(_ answer: String) -> String? {
        let trimmed = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
            !trimmed.contains("\0"),
            !trimmed.contains("\n"),
            trimmed.split(separator: ":").contains(where: { $0.hasPrefix("/") })
        else { return nil }
        return trimmed
    }

    /// Run the login shell and take its answer, or nothing at all.
    ///
    /// `-l` is what makes the profile run; without it this reads the same
    /// truncated `PATH` we already have. stdin is `/dev/null` and stderr is
    /// discarded, so a profile that prompts cannot block us and a profile that
    /// complains cannot be mistaken for the answer.
    public static let readFromLoginShell: Reader = { shell, timeout in
        guard FileManager.default.isExecutableFile(atPath: shell) else { return nil }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        process.arguments = ["-lc", script]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice

        do { try process.run() } catch { return nil }

        let collected = ReadToEnd(handle: output.fileHandleForReading)
        guard collected.wait(timeout) else {
            // A profile that hangs must not hang the engine's supervisor with
            // it. `terminate` first, because a shell given the chance usually
            // takes it; `SIGKILL` is what makes the bound real.
            process.terminate()
            if !process.waitUntil(deadline: .now() + 0.2) {
                kill(process.processIdentifier, SIGKILL)
            }
            return nil
        }
        process.waitUntilExit()
        guard process.terminationStatus == 0, process.terminationReason == .exit else { return nil }
        return String(data: collected.data, encoding: .utf8)
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
            usleep(10_000)
        }
        return !isRunning
    }
}
