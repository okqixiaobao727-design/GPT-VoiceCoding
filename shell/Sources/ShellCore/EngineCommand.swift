import Foundation

/// What the shell spawns, and where that interpreter came from.
///
/// One resolver, because the bundled interpreter's name belongs to the app-bundle
/// pipeline and a name sprinkled through the shell would mean editing it in five
/// places. It stays in ``BundleLayout``.
public struct EngineCommand: Equatable, Sendable {
    public enum Source: Equatable, Sendable {
        /// Inside the app bundle. ADR 0005: containment is what earns the
        /// microphone grant, so this is the shipping shape.
        case bundled
        /// Named outright by the environment, for a checkout that is not the
        /// interpreter first on the path.
        case named
        /// Found on `PATH`. The developer path, which is a **stated feature**:
        /// headless mode stays real, and the shell must be able to drive a
        /// working copy rather than only a bundle.
        case developerPath
    }

    public var executable: String
    public var arguments: [String]
    public var source: Source

    /// Which interpreter to use when there is no bundle. Named rather than
    /// guessed, because a checkout's virtual environment is not on `PATH`.
    public static let interpreterVariable = "GPTVOICECODING_ENGINE_PYTHON"

    /// The engine's module, run in the foreground. It never daemonises, so
    /// process parenthood holds (ADR 0005).
    public static let module = "gpt_voicecoding.engine"

    public static func resolve(
        resources: URL?,
        configPath: String,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        searchPath: [String]? = nil
    ) throws -> EngineCommand {
        let arguments = ["-m", module, "--config", configPath]

        if let resources {
            let bundled = resources.appendingPathComponent(
                BundleLayout.engineInterpreterRelativePath
            ).path
            if isExecutable(bundled) {
                return EngineCommand(executable: bundled, arguments: arguments, source: .bundled)
            }
        }

        if let named = environment[interpreterVariable], !named.isEmpty {
            guard isExecutable(named) else {
                throw EngineCommandFailure.namedInterpreterMissing(
                    "\(interpreterVariable) names \(named), which is not an executable file")
            }
            return EngineCommand(executable: named, arguments: arguments, source: .named)
        }

        let path =
            searchPath
            ?? (environment["PATH"] ?? "/usr/bin:/bin").split(separator: ":").map(String.init)
        for directory in path {
            let candidate = URL(fileURLWithPath: directory).appendingPathComponent("python3").path
            if isExecutable(candidate) {
                return EngineCommand(
                    executable: candidate, arguments: arguments, source: .developerPath)
            }
        }

        throw EngineCommandFailure.noInterpreter(
            "no engine interpreter: none is bundled, \(interpreterVariable) names none, "
                + "and no python3 is on the path")
    }

    private static func isExecutable(_ path: String) -> Bool {
        var metadata = stat()
        guard stat(path, &metadata) == 0, metadata.st_mode & S_IFMT == S_IFREG else { return false }
        return access(path, X_OK) == 0
    }
}

public enum EngineCommandFailure: Error, Equatable, Sendable {
    case noInterpreter(String)
    case namedInterpreterMissing(String)

    public var detail: String {
        switch self {
        case .noInterpreter(let detail), .namedInterpreterMissing(let detail): return detail
        }
    }
}
