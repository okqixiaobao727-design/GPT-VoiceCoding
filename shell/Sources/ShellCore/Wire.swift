import Foundation

/// The byte bound both sides read from one place. `docs/control-plane.md`: a line
/// may not exceed this in either direction, because there is no honest way to
/// resync inside a line.
public let maxRequestBytes = 65536

/// The control-plane protocol this shell can interpret. Held to the engine's
/// declaration by the cross-language agreement test in `tests/test_app_bundle.py`.
public let controlPlaneProtocolVersion = 3

/// Every action this engine has. Nine, and the set is closed — adding one is a
/// contract change, so the shell names them rather than composing strings.
public enum Action: String, Sendable, CaseIterable {
    case status
    case sessions
    case `switch`
    /// The Live Toggle. One action: it ends the call the system owns, or starts
    /// one if none is up. Bridge Core owns that policy; no surface holds call state.
    case live
    case launch
    case close
    case relay
    case approve
    case verify
}

public struct Request: Sendable {
    public var action: Action
    public var payload: [String: JSONValue]?

    public init(action: Action, payload: [String: JSONValue]? = nil) {
        self.action = action
        self.payload = payload
    }

    /// Convenience for tests and for naming an action this engine may not have.
    init(action: String, payload: [String: JSONValue]? = nil) {
        self.init(action: Action(rawValue: action) ?? .status, payload: payload)
    }

    public func line() throws -> Data {
        var document: [String: Any] = ["action": action.rawValue]
        if let payload {
            // Omitted rather than sent empty when an action takes nothing.
            document["payload"] = payload.mapValues(\.raw)
        }
        return try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
    }

    public func terminatedLine() throws -> Data {
        var line = try self.line()
        line.append(UInt8(ascii: "\n"))
        return line
    }
}

/// The engine's own refusal codes. `engine_unreachable` is deliberately absent:
/// it is raised by a surface and never sent by the engine, so a wire code by that
/// name would let the shell render a message it wrote itself as one Bridge Core
/// said.
public enum ErrorCode: Equatable, Sendable {
    case malformedRequest
    case unknownAction
    case invalidPayload
    case unknownSwitch
    case unknownSession
    case staleSession
    case unknownPending
    case secondCallRefused
    case launchFailed
    case closeFailed
    case seamUnavailable
    case refused
    /// A code this shell does not know. Still carried, because an engine that
    /// grew one has still refused, and the user is owed its words.
    case other(String)

    public init(wire: String) {
        switch wire {
        case "malformed_request": self = .malformedRequest
        case "unknown_action": self = .unknownAction
        case "invalid_payload": self = .invalidPayload
        case "unknown_switch": self = .unknownSwitch
        case "unknown_session": self = .unknownSession
        case "stale_session": self = .staleSession
        case "unknown_pending": self = .unknownPending
        case "second_call_refused": self = .secondCallRefused
        case "launch_failed": self = .launchFailed
        case "close_failed": self = .closeFailed
        case "seam_unavailable": self = .seamUnavailable
        case "refused": self = .refused
        default: self = .other(wire)
        }
    }
}

/// Something Bridge Core refused, in Bridge Core's own words.
public struct Refusal: Equatable, Sendable, Error {
    public var code: ErrorCode
    /// **Rendered verbatim.** The honest phrasing lives in one place on purpose.
    public var message: String

    public init(code: ErrorCode, message: String) {
        self.code = code
        self.message = message
    }
}

public struct Reply: Sendable {
    public var ok: Bool
    /// `null` when the line never named a usable action.
    public var action: String?
    /// `nil` means the reply did not carry a numeric protocol version: a missing
    /// field and JSON `null` decode alike. The socket client treats that and any
    /// unsupported numeric version as a protocol mismatch.
    public var protocolVersion: Int?
    public var data: [String: JSONValue]
    public var refusal: Refusal?

    public static func of(_ line: Data) throws -> Reply {
        let raw: Any
        do {
            raw = try JSONSerialization.jsonObject(with: line, options: [.fragmentsAllowed])
        } catch {
            throw ControlPlaneFailure.unreadable("the engine answered unreadably: \(error)")
        }
        guard case .object(let document) = JSONValue.of(raw) else {
            throw ControlPlaneFailure.unreadable("the engine answered with something not an object")
        }
        var refusal: Refusal?
        if let error = document["error"]?.object {
            refusal = Refusal(
                code: ErrorCode(wire: error["code"]?.string ?? ""),
                message: error["message"]?.string ?? "")
        }
        return Reply(
            ok: document["ok"]?.bool ?? false,
            action: document["action"]?.string,
            protocolVersion: document["protocol"]?.number.map(Int.init),
            data: document["data"]?.object ?? [:],
            refusal: refusal)
    }
}

/// What can go wrong on this side of the wire.
public enum ControlPlaneFailure: Error, Equatable, Sendable {
    /// Nothing answered — `engine_unreachable`. A **surface-side** condition, and
    /// phrased so it can never be mistaken for something the engine said.
    case engineUnreachable(String)
    /// The engine answered, but not in the protocol this shell can interpret.
    case protocolMismatch(received: Int?, supported: Int)
    /// An answer arrived that this protocol cannot represent.
    case unreadable(String)

    public var detail: String {
        switch self {
        case .engineUnreachable(let detail), .unreadable(let detail): return detail
        case .protocolMismatch(let received, let supported):
            guard let received else {
                return "the engine did not declare a control-plane protocol version; "
                    + "this shell supports version \(supported)"
            }
            return "the engine speaks control-plane protocol version \(received); "
                + "this shell supports version \(supported)"
        }
    }
}
