import Darwin
import Foundation

/// Where the engine's configuration is, and where its socket is.
///
/// The socket path is **not** derivable from the state path: Darwin caps an
/// `AF_UNIX` path at 103 bytes and the application-support path is already 76 of
/// them, so the socket lives in a short per-uid runtime root instead. A surface
/// reads it from the same configuration file it spawns the engine with, or is
/// told it directly.
///
/// The default below is mirrored from `docs/control-plane.md`, which is the
/// canonical statement of it; `gpt_voicecoding/config.py` implements the same
/// sentence. Neither implementation is authoritative over the other.
public struct EngineLocation: Sendable, Equatable {
    public var configPath: String
    public var socketPath: String

    public init(configPath: String, socketPath: String) {
        self.configPath = configPath
        self.socketPath = socketPath
    }

    /// The engine's own application-support directory, where it keeps the file
    /// the user owns and the engine only reads.
    public static func defaultConfigPath() -> String {
        NSHomeDirectory()
            + "/Library/Application Support/GPT-VoiceCoding/engine/config.toml"
    }

    /// Per-uid rather than shared, so two accounts on one machine each get their
    /// own engine rather than one refusing the other's socket.
    public static func defaultSocketPath(uid: uid_t? = nil) -> String {
        "/tmp/gpt-voicecoding-\(uid ?? getuid())/control.sock"
    }

    /// Read the socket path out of the configuration, falling back to the
    /// documented default only when the file or the key is genuinely absent.
    ///
    /// An unreadable *value* is a refusal, not a fallback: a misspelled setting
    /// that silently defaults would send the shell to the wrong socket and have
    /// it report a missing engine that was running all along.
    public static func resolve(configPath: String = defaultConfigPath()) throws -> EngineLocation {
        guard let text = try? String(contentsOfFile: configPath, encoding: .utf8) else {
            // The engine refuses to start without a configuration and says why on
            // stderr. Showing that is the shell's job; inventing a second
            // complaint about the same file is not.
            return EngineLocation(configPath: configPath, socketPath: defaultSocketPath())
        }
        guard
            let configured = try MinimalTOML.string(
                forKey: "socket_path", inTable: "engine", of: text)
        else {
            return EngineLocation(configPath: configPath, socketPath: defaultSocketPath())
        }
        let trimmed = configured.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else {
            throw ConfigurationFailure.unreadable("[engine] socket_path must be a path")
        }
        return EngineLocation(configPath: configPath, socketPath: expandingTilde(trimmed))
    }

    static func expandingTilde(_ path: String) -> String {
        guard path == "~" || path.hasPrefix("~/") else { return path }
        return NSHomeDirectory() + path.dropFirst(1)
    }
}
