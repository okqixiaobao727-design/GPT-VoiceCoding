import Darwin
import Foundation

/// The shell-owned Telegram credential, persisted as an engine environment.
///
/// The interface answers the two questions its callers have: whether the named
/// token is fit to start the engine with, and which file-backed variables belong
/// in that child's environment. The token itself never appears in a state or an
/// error sentence.
///
/// Adapted from `legacy@1d32845:bridge-serve:103-139`: the proven effect is a
/// credentials file supplying named environment variables and a missing value
/// stopping launch. Here the Swift parent owns that work, reads no shell syntax,
/// and adds the private-file and menu-bar contracts legacy did not have.
public struct TelegramCredentials: Sendable {
    public enum UnsafeReason: Equatable, Sendable {
        case permissions(path: String)
    }

    public enum FileProblem: Equatable, Sendable {
        case notUTF8
        case readFailed(String)
        case missingAssignment(line: Int)
        case invalidVariableName(line: Int)
        case duplicateVariable(line: Int, name: String)
    }

    public enum UnreadableReason: Equatable, Sendable {
        case configuration(path: String, reason: String)
        case inspection(path: String, code: Int32)
        case environment(path: String, problem: FileProblem)
    }

    public enum State: Equatable, Sendable {
        case ready
        case notConfigured
        case missing
        case unsafe(UnsafeReason)
        case unreadable(UnreadableReason)

        public var failureDetail: String? {
            TelegramCredentialSentence.state(self)
        }

        public var allowsEngineStart: Bool {
            switch self {
            case .ready, .notConfigured: return true
            case .missing, .unsafe, .unreadable: return false
            }
        }
    }

    public struct Reading: Equatable, Sendable {
        public let state: State
        public let environment: [String: String]
    }

    public let configPath: String
    public let environmentPath: String

    public init(
        configPath: String = EngineLocation.defaultConfigPath(),
        environmentPath: String? = nil
    ) {
        self.configPath = configPath
        self.environmentPath =
            environmentPath
            ?? URL(fileURLWithPath: configPath).deletingLastPathComponent()
            .appendingPathComponent("environment").path
    }

    private enum ConfigurationReading {
        case named(String)
        case notConfigured
        case failed(State)
    }

    /// The one mapping from configuration-reader failures into this module's
    /// state. Both load and save consume the same typed result.
    private func configurationReading() -> ConfigurationReading {
        do {
            guard let variable = try Self.configuredTokenVariable(in: configPath) else {
                return .notConfigured
            }
            return .named(variable)
        } catch let failure as ConfigurationFailure {
            return .failed(
                .unreadable(.configuration(path: configPath, reason: failure.detail)))
        } catch is InvalidTokenVariable {
            return .failed(
                .unreadable(
                    .configuration(
                        path: configPath,
                        reason: "[adapters.settings.companion_channel] token_env must name an "
                            + "environment variable")))
        } catch {
            return .failed(
                .unreadable(
                    .configuration(path: configPath, reason: error.localizedDescription)))
        }
    }

    public func load() -> Reading {
        let tokenVariable: String
        switch configurationReading() {
        case .named(let variable): tokenVariable = variable
        case .notConfigured: return Reading(state: .notConfigured, environment: [:])
        case .failed(let state): return Reading(state: state, environment: [:])
        }

        var metadata = stat()
        guard lstat(environmentPath, &metadata) == 0 else {
            if errno == ENOENT { return Reading(state: .missing, environment: [:]) }
            return Reading(
                state: .unreadable(.inspection(path: environmentPath, code: errno)),
                environment: [:])
        }
        guard metadata.st_mode & 0o077 == 0 else {
            return Reading(
                state: .unsafe(.permissions(path: environmentPath)),
                environment: [:])
        }

        let environment: [String: String]
        do {
            environment = try Self.environment(in: Self.utf8File(at: environmentPath))
        } catch let failure as CredentialFileFailure {
            return Reading(
                state: .unreadable(
                    .environment(path: environmentPath, problem: failure.problem)),
                environment: [:])
        } catch {
            return Reading(
                state: .unreadable(
                    .environment(
                        path: environmentPath,
                        problem: .readFailed(error.localizedDescription))),
                environment: [:])
        }

        guard let token = environment[tokenVariable],
            !token.trimmingCharacters(
                in: .whitespacesAndNewlines
            ).isEmpty
        else {
            return Reading(state: .missing, environment: environment)
        }
        return Reading(state: .ready, environment: environment)
    }

    /// Save one write-only token without discarding any other environment value.
    ///
    /// The replacement is born private beside the destination, then renamed over
    /// it. There is no instant at which the credential exists in a group-readable
    /// file, and the rename is the one visible change a concurrent launch can see.
    public func save(token: String) throws -> Reading {
        guard !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw TelegramCredentialSaveFailure.emptyToken
        }
        guard !token.contains("\n"), !token.contains("\r"), !token.contains("\0") else {
            throw TelegramCredentialSaveFailure.invalidTokenCharacters
        }

        let variable: String
        switch configurationReading() {
        case .named(let configured): variable = configured
        case .notConfigured: throw TelegramCredentialSaveFailure.tokenNotConfigured
        case .failed(let state): throw TelegramCredentialSaveFailure.refused(state)
        }

        let current = load()
        var environment: [String: String]
        switch current.state {
        case .ready, .missing:
            environment = current.environment
        case .notConfigured:
            throw TelegramCredentialSaveFailure.tokenNotConfigured
        case .unsafe, .unreadable:
            throw TelegramCredentialSaveFailure.refused(current.state)
        }
        environment[variable] = token
        let original: String?
        if FileManager.default.fileExists(atPath: environmentPath) {
            do {
                original = try Self.utf8File(at: environmentPath)
            } catch let failure as CredentialFileFailure {
                throw TelegramCredentialSaveFailure.refused(
                    .unreadable(
                        .environment(path: environmentPath, problem: failure.problem)))
            } catch {
                throw TelegramCredentialSaveFailure.refused(
                    .unreadable(
                        .environment(
                            path: environmentPath,
                            problem: .readFailed(error.localizedDescription))))
            }
        } else {
            original = nil
        }
        let rendered = Self.replacing(variable, with: token, in: original)

        do {
            try Self.replaceFile(at: environmentPath, with: Data(rendered.utf8))
        } catch {
            throw TelegramCredentialSaveFailure.writeFailed(
                path: environmentPath, reason: error.localizedDescription)
        }
        return Reading(state: .ready, environment: environment)
    }

    /// Absence means this engine configuration does not request Telegram. A
    /// missing or unreadable configuration is likewise left to the engine,
    /// whose own preflight sentence the shell already presents. A malformed
    /// token_env that is actually present remains this boundary's refusal.
    private static func configuredTokenVariable(in configPath: String) throws -> String? {
        guard
            let data = try? Data(contentsOf: URL(fileURLWithPath: configPath)),
            let config = String(data: data, encoding: .utf8),
            let named = try MinimalTOML.string(
                forKey: "token_env",
                inTable: "adapters.settings.companion_channel",
                of: config)
        else { return nil }
        guard isEnvironmentName(named) else {
            throw InvalidTokenVariable()
        }
        return named
    }

    private static func utf8File(at path: String) throws -> String {
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        guard let text = String(data: data, encoding: .utf8) else {
            throw CredentialFileFailure(.notUTF8)
        }
        return text
    }

    private static func environment(in text: String) throws -> [String: String] {
        var environment: [String: String] = [:]
        for (offset, rawLine) in text.split(
            separator: "\n", omittingEmptySubsequences: false
        ).enumerated() {
            let line = String(rawLine)
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { continue }
            guard let separator = line.firstIndex(of: "=") else {
                throw CredentialFileFailure(.missingAssignment(line: offset + 1))
            }
            let name = String(line[..<separator])
            guard isEnvironmentName(name) else {
                throw CredentialFileFailure(.invalidVariableName(line: offset + 1))
            }
            guard environment[name] == nil else {
                throw CredentialFileFailure(.duplicateVariable(line: offset + 1, name: name))
            }
            environment[name] = String(line[line.index(after: separator)...])
        }
        return environment
    }

    private static func replacing(
        _ variable: String, with token: String, in original: String?
    ) -> String {
        guard let original else { return "\(variable)=\(token)\n" }
        var lines = original.split(separator: "\n", omittingEmptySubsequences: false).map(
            String.init)
        var replaced = false
        for index in lines.indices {
            guard
                let separator = lines[index].firstIndex(of: "="),
                lines[index][..<separator] == variable
            else { continue }
            lines[index] = "\(variable)=\(token)"
            replaced = true
        }
        if replaced { return lines.joined(separator: "\n") }
        if original.isEmpty { return "\(variable)=\(token)\n" }
        return original + (original.hasSuffix("\n") ? "" : "\n") + "\(variable)=\(token)\n"
    }

    private static func isEnvironmentName(_ name: String) -> Bool {
        guard let first = name.utf8.first, Self.isLetter(first) || first == 95 else { return false }
        return name.utf8.dropFirst().allSatisfy {
            Self.isLetter($0) || (48...57).contains($0) || $0 == 95
        }
    }

    private static func isLetter(_ byte: UInt8) -> Bool {
        (65...90).contains(byte) || (97...122).contains(byte)
    }

    private static func replaceFile(at path: String, with data: Data) throws {
        let destination = URL(fileURLWithPath: path)
        let directory = destination.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let temporary = directory.appendingPathComponent(".environment-\(UUID().uuidString).tmp")
        let descriptor = open(temporary.path, O_WRONLY | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else { throw POSIXFailure(code: errno) }
        var shouldRemove = true
        defer {
            close(descriptor)
            if shouldRemove { unlink(temporary.path) }
        }

        try data.withUnsafeBytes { raw in
            guard var address = raw.baseAddress else { return }
            var remaining = raw.count
            while remaining > 0 {
                let written = Darwin.write(descriptor, address, remaining)
                guard written > 0 else { throw POSIXFailure(code: written < 0 ? errno : EIO) }
                remaining -= written
                address = address.advanced(by: written)
            }
        }
        guard fsync(descriptor) == 0 else { throw POSIXFailure(code: errno) }
        guard rename(temporary.path, path) == 0 else { throw POSIXFailure(code: errno) }
        shouldRemove = false
    }
}

private struct InvalidTokenVariable: Error {}

private struct CredentialFileFailure: Error {
    let problem: TelegramCredentials.FileProblem

    init(_ problem: TelegramCredentials.FileProblem) { self.problem = problem }
}

private struct POSIXFailure: LocalizedError {
    let code: Int32

    var errorDescription: String? { String(cString: strerror(code)) }
}

public enum TelegramCredentialSaveFailure: Error, Equatable, Sendable {
    case emptyToken
    case invalidTokenCharacters
    case tokenNotConfigured
    case refused(TelegramCredentials.State)
    case writeFailed(path: String, reason: String)

    public var detail: String {
        TelegramCredentialSentence.save(self)
    }
}

public struct TelegramCredentialPreflightFailure: Error, Equatable, Sendable,
    CustomStringConvertible
{
    public let state: TelegramCredentials.State

    public init(_ state: TelegramCredentials.State) { self.state = state }
    public var description: String {
        TelegramCredentialSentence.state(state) ?? "Telegram credentials are unavailable"
    }
}

private enum TelegramCredentialSentence {
    static func state(_ state: TelegramCredentials.State) -> String? {
        switch state {
        case .ready, .notConfigured: return nil
        case .missing: return "Telegram token not set"
        case .unsafe(.permissions(let path)):
            return "Telegram credentials at \(path) must be private like mode 0600; "
                + "group or other permissions are not allowed"
        case .unreadable(.configuration(let path, let reason)):
            return "The engine configuration at \(path) could not be read: \(reason)"
        case .unreadable(.inspection(_, let code)):
            return "Telegram credentials could not be inspected: \(String(cString: strerror(code)))"
        case .unreadable(.environment(let path, let problem)):
            return environment(path: path, problem: problem)
        }
    }

    static func save(_ failure: TelegramCredentialSaveFailure) -> String {
        switch failure {
        case .emptyToken: return "The Telegram bot token cannot be empty"
        case .invalidTokenCharacters:
            return "The Telegram bot token cannot contain a line break or null byte"
        case .tokenNotConfigured:
            return "[adapters.settings.companion_channel] token_env is not configured"
        case .refused(let state):
            return Self.state(state) ?? "Telegram credentials are unavailable"
        case .writeFailed(let path, let reason):
            return "Telegram credentials at \(path) could not be saved: \(reason)"
        }
    }

    private static func environment(
        path: String, problem: TelegramCredentials.FileProblem
    ) -> String {
        switch problem {
        case .notUTF8: return "The file at \(path) is not UTF-8"
        case .readFailed(let reason):
            return "Telegram credentials at \(path) could not be read: \(reason)"
        case .missingAssignment(let line):
            return "Telegram credentials line \(line) must be KEY=VALUE"
        case .invalidVariableName(let line):
            return "Telegram credentials line \(line) has an invalid variable name"
        case .duplicateVariable(let line, let name):
            return "Telegram credentials line \(line) repeats \(name)"
        }
    }
}
