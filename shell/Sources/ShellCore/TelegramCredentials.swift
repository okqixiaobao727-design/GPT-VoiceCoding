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
    public enum State: Equatable, Sendable {
        case ready
        case notConfigured
        case missing
        case unsafe(String)
        case unreadable(String)

        public var failureDetail: String? {
            switch self {
            case .ready, .notConfigured: return nil
            case .missing: return "Telegram token not set"
            case .unsafe(let detail), .unreadable(let detail): return detail
            }
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

    public func load() -> Reading {
        let tokenVariable: String?
        do {
            tokenVariable = try Self.configuredTokenVariable(in: configPath)
        } catch let failure as ConfigurationFailure {
            return Reading(state: .unreadable(failure.detail), environment: [:])
        } catch let failure as CredentialFileFailure {
            return Reading(state: .unreadable(failure.detail), environment: [:])
        } catch {
            return Reading(
                state: .unreadable(
                    "The engine configuration at \(configPath) could not be read: "
                        + error.localizedDescription),
                environment: [:])
        }
        guard let tokenVariable else {
            return Reading(state: .notConfigured, environment: [:])
        }

        var metadata = stat()
        guard lstat(environmentPath, &metadata) == 0 else {
            if errno == ENOENT { return Reading(state: .missing, environment: [:]) }
            return Reading(
                state: .unreadable(
                    "Telegram credentials could not be inspected: \(String(cString: strerror(errno)))"
                ),
                environment: [:])
        }
        guard metadata.st_mode & 0o077 == 0 else {
            return Reading(
                state: .unsafe(
                    "Telegram credentials at \(environmentPath) must be private like mode 0600; "
                        + "group or other permissions are not allowed"),
                environment: [:])
        }

        let environment: [String: String]
        do {
            environment = try Self.environment(in: Self.utf8File(at: environmentPath))
        } catch let failure as CredentialFileFailure {
            return Reading(state: .unreadable(failure.detail), environment: [:])
        } catch {
            return Reading(
                state: .unreadable(
                    "Telegram credentials at \(environmentPath) could not be read: "
                        + error.localizedDescription),
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
            throw TelegramCredentialSaveFailure.invalid("The Telegram bot token cannot be empty")
        }
        guard !token.contains("\n"), !token.contains("\r"), !token.contains("\0") else {
            throw TelegramCredentialSaveFailure.invalid(
                "The Telegram bot token cannot contain a line break or null byte")
        }

        let variable: String
        do {
            guard let configured = try Self.configuredTokenVariable(in: configPath) else {
                throw CredentialFileFailure(
                    "[adapters.settings.companion_channel] token_env is not configured")
            }
            variable = configured
        } catch let failure as ConfigurationFailure {
            throw TelegramCredentialSaveFailure.refused(failure.detail)
        } catch let failure as CredentialFileFailure {
            throw TelegramCredentialSaveFailure.refused(failure.detail)
        } catch {
            throw TelegramCredentialSaveFailure.refused(
                "The engine configuration at \(configPath) could not be read: "
                    + error.localizedDescription)
        }

        let current = load()
        var environment: [String: String]
        switch current.state {
        case .ready, .missing:
            environment = current.environment
        case .notConfigured:
            throw TelegramCredentialSaveFailure.refused(
                "[adapters.settings.companion_channel] token_env is not configured")
        case .unsafe(let detail), .unreadable(let detail):
            throw TelegramCredentialSaveFailure.refused(detail)
        }
        environment[variable] = token
        let original: String?
        if FileManager.default.fileExists(atPath: environmentPath) {
            do {
                original = try Self.utf8File(at: environmentPath)
            } catch let failure as CredentialFileFailure {
                throw TelegramCredentialSaveFailure.refused(failure.detail)
            } catch {
                throw TelegramCredentialSaveFailure.refused(
                    "Telegram credentials at \(environmentPath) could not be read: "
                        + error.localizedDescription)
            }
        } else {
            original = nil
        }
        let rendered = Self.replacing(variable, with: token, in: original)

        do {
            try Self.replaceFile(at: environmentPath, with: Data(rendered.utf8))
        } catch {
            throw TelegramCredentialSaveFailure.write(
                "Telegram credentials could not be saved: \(error.localizedDescription)")
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
            throw CredentialFileFailure(
                "[adapters.settings.companion_channel] token_env must name an "
                    + "environment variable")
        }
        return named
    }

    private static func utf8File(at path: String) throws -> String {
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        guard let text = String(data: data, encoding: .utf8) else {
            throw CredentialFileFailure("The file at \(path) is not UTF-8")
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
                throw CredentialFileFailure(
                    "Telegram credentials line \(offset + 1) must be KEY=VALUE")
            }
            let name = String(line[..<separator])
            guard isEnvironmentName(name) else {
                throw CredentialFileFailure(
                    "Telegram credentials line \(offset + 1) has an invalid variable name")
            }
            guard environment[name] == nil else {
                throw CredentialFileFailure(
                    "Telegram credentials line \(offset + 1) repeats \(name)")
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

private struct CredentialFileFailure: Error {
    let detail: String

    init(_ detail: String) { self.detail = detail }
}

private struct POSIXFailure: LocalizedError {
    let code: Int32

    var errorDescription: String? { String(cString: strerror(code)) }
}

public enum TelegramCredentialSaveFailure: Error, Equatable, Sendable {
    case invalid(String)
    case refused(String)
    case write(String)

    public var detail: String {
        switch self {
        case .invalid(let detail), .refused(let detail), .write(let detail): return detail
        }
    }
}

public struct TelegramCredentialPreflightFailure: Error, Equatable, Sendable,
    CustomStringConvertible
{
    public let detail: String

    public init(_ detail: String) { self.detail = detail }
    public var description: String { detail }
}
